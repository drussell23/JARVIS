#!/usr/bin/env python3
"""
ML Model Loading Real-Time Logging Configuration
===============================================

Provides detailed, colorized console logging for ML model management
to visualize smart lazy loading in real-time.
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from colorama import Fore, Back, Style
import threading

# No colorama.init(): it replaces sys.stdout process-wide, and this module is
# imported inside the supervisor, whose terminal streams are not its to wrap.
# The Fore/Style codes below are plain ANSI, which current terminals render.


class MLModelLogger:
    """Enhanced logger for ML model loading events"""
    
    def __init__(self, name: str = "ml_models"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        # Records go to the root logger's handlers, which the host process
        # owns (the supervisor's terminal view and session log). A private
        # stdout handler here printed every record twice -- once itself,
        # once through root -- and around the terminal's view.
        
        # Thread-local storage for context
        self.thread_local = threading.local()
        
    def set_context(self, **kwargs):
        """Set context for current thread"""
        for key, value in kwargs.items():
            setattr(self.thread_local, key, value)
            
    def _log_with_context(self, level: int, msg: str, event_type: str, **kwargs):
        """Log with context and extra fields"""
        extra = {
            'event_type': event_type,
            'model_name': kwargs.get('model_name', ''),
            'memory_info': kwargs.get('memory_info', {}),
            'context': kwargs.get('context', '')
        }
        
        # Add thread-local context
        for attr in ['model_name', 'context']:
            if hasattr(self.thread_local, attr) and attr not in kwargs:
                extra[attr] = getattr(self.thread_local, attr)
                
        self.logger.log(level, msg, extra=extra)
        
    def load_start(self, model_name: str, size_mb: float, reason: str = ""):
        """Log model load start"""
        msg = f"Loading model (size: {size_mb:.1f}MB)"
        if reason:
            msg += f" - Reason: {reason}"
        self._log_with_context(logging.INFO, msg, 'LOAD_START', model_name=model_name)
        
    def load_success(self, model_name: str, load_time_s: float, memory_info: Dict[str, Any]):
        """Log successful model load"""
        msg = f"Model loaded in {load_time_s:.2f}s"
        self._log_with_context(logging.INFO, msg, 'LOAD_SUCCESS', 
                              model_name=model_name, memory_info=memory_info)
        
    def load_failed(self, model_name: str, error: str, memory_info: Dict[str, Any]):
        """Log failed model load"""
        msg = f"Failed to load model: {error}"
        self._log_with_context(logging.ERROR, msg, 'LOAD_FAILED',
                              model_name=model_name, memory_info=memory_info)
        
    def unload(self, model_name: str, freed_mb: float, reason: str = ""):
        """Log model unload"""
        msg = f"Unloading model (freeing {freed_mb:.1f}MB)"
        if reason:
            msg += f" - Reason: {reason}"
        self._log_with_context(logging.INFO, msg, 'UNLOAD', model_name=model_name)
        
    def cache_hit(self, model_name: str):
        """Log cache hit"""
        msg = "Model retrieved from cache (0ms)"
        self._log_with_context(logging.INFO, msg, 'CACHE_HIT', model_name=model_name)
        
    def memory_check(self, memory_info: Dict[str, Any], decision: str):
        """Log memory check"""
        msg = f"Memory check - Decision: {decision}"
        self._log_with_context(logging.INFO, msg, 'MEMORY_CHECK', memory_info=memory_info)
        
    def context_change(self, old_context: str, new_context: str, models_affected: int):
        """Log context change"""
        msg = f"Context changed: {old_context} → {new_context} ({models_affected} models affected)"
        self._log_with_context(logging.INFO, msg, 'CONTEXT_CHANGE', context=new_context)
        
    def proximity_change(self, proximity: str, distance_m: Optional[float], action: str):
        """Log proximity change"""
        dist_str = f"{distance_m:.1f}m" if distance_m else proximity
        msg = f"Proximity: {dist_str} - Action: {action}"
        self._log_with_context(logging.INFO, msg, 'PROXIMITY')
        
    def cleanup_triggered(self, reason: str, models_to_unload: int):
        """Log cleanup trigger"""
        msg = f"Cleanup triggered ({reason}) - Unloading {models_to_unload} models"
        self._log_with_context(logging.WARNING, msg, 'CLEANUP')
        
    def quantized_load(self, model_name: str, original_mb: float, quantized_mb: float):
        """Log quantized model load"""
        reduction = (1 - quantized_mb/original_mb) * 100
        msg = f"Loading quantized model: {original_mb:.1f}MB → {quantized_mb:.1f}MB ({reduction:.0f}% reduction)"
        self._log_with_context(logging.INFO, msg, 'QUANTIZED', model_name=model_name)
        
    def prediction(self, predicted_models: List[str], confidence: float):
        """Log model prediction"""
        msg = f"Predicted next models: {', '.join(predicted_models)} (confidence: {confidence:.0f}%)"
        self._log_with_context(logging.INFO, msg, 'PREDICT')
        
    def critical_memory(self, percent: float, action: str):
        """Log critical memory situation"""
        msg = f"CRITICAL MEMORY: {percent:.1f}% - {action}"
        self._log_with_context(logging.CRITICAL, msg, 'CRITICAL')


class MemoryVisualizationLogger:
    """ASCII-based memory visualization for console"""
    
    def __init__(self, ml_logger: MLModelLogger):
        self.ml_logger = ml_logger
        self.last_visualization = ""
        
    def visualize_memory(self, memory_info: Dict[str, Any], models: Dict[str, Dict[str, Any]]):
        """Create ASCII visualization of memory usage"""
        total_gb = 16  # 16GB system
        target_percent = 35
        
        # Calculate percentages
        system_percent = memory_info.get('system_percent', 0)
        ml_percent = memory_info.get('ml_percent', 0)
        
        # Create memory bar
        bar_width = 50
        filled = int(bar_width * system_percent / 100)
        ml_filled = int(bar_width * ml_percent / 100)
        target_pos = int(bar_width * target_percent / 100)
        
        # Build visualization
        lines = [
            "",
            "=" * 70,
            "JARVIS ML MEMORY MONITOR",
            "=" * 70,
            "",
            f"System Memory Usage: {system_percent:.1f}% of 16GB",
            self._create_bar(filled, ml_filled, target_pos, bar_width),
            "",
            "Legend: █=System ▓=ML Models |=35% Target",
            "",
            "Loaded Models:",
            "-" * 30
        ]
        
        # Add loaded models
        if models:
            for name, info in models.items():
                size_mb = info.get('size_mb', 0)
                last_used = info.get('last_used_s', 0)
                quantized = "Q" if info.get('quantized', False) else " "
                lines.append(f"  [{quantized}] {name:<20} {size_mb:>6.1f}MB  (used {last_used:.0f}s ago)")
        else:
            lines.append("  (No models loaded)")
            
        lines.extend([
            "-" * 30,
            f"Total ML Memory: {memory_info.get('ml_models_mb', 0):.1f}MB / 2048MB budget",
            ""
        ])
        
        # Print visualization
        visualization = "\n".join(lines)
        
        # Only print if changed
        if visualization != self.last_visualization:
            # Clear previous lines (approximate)
            print("\033[2J\033[H")  # Clear screen and move cursor to top
            
            # Color the output
            for line in lines:
                if "CRITICAL" in line or system_percent > 35:
                    print(Fore.RED + line + Style.RESET_ALL)
                elif "WARNING" in line or system_percent > 25:
                    print(Fore.YELLOW + line + Style.RESET_ALL)
                elif "█" in line or "▓" in line:
                    # Color the bar
                    colored_line = line
                    if system_percent > 35:
                        colored_line = Fore.RED + line + Style.RESET_ALL
                    elif system_percent > 25:
                        colored_line = Fore.YELLOW + line + Style.RESET_ALL
                    else:
                        colored_line = Fore.GREEN + line + Style.RESET_ALL
                    print(colored_line)
                else:
                    print(line)
                    
            self.last_visualization = visualization
            
    def _create_bar(self, filled: int, ml_filled: int, target_pos: int, width: int) -> str:
        """Create memory usage bar"""
        bar = []
        
        for i in range(width):
            if i < ml_filled:
                bar.append("▓")  # ML models
            elif i < filled:
                bar.append("█")  # Other system memory
            elif i == target_pos:
                bar.append("|")  # Target marker
            else:
                bar.append("░")  # Empty
                
        return f"[{''.join(bar)}]"


# Global logger instance
ml_logger = MLModelLogger()
memory_visualizer = MemoryVisualizationLogger(ml_logger)


def setup_ml_logging(level: int = logging.INFO, enable_visualization: bool = True):
    """Setup ML model logging with optional visualization"""
    ml_logger.logger.setLevel(level)
    
    if enable_visualization:
        # Start visualization thread
        import threading
        import time
        
        def visualization_loop():
            while True:
                try:
                    # Get current memory stats (would be passed from ml_memory_manager)
                    # This is a placeholder - in real use, this would get actual stats
                    time.sleep(5)
                except Exception as e:
                    ml_logger.logger.error(f"Visualization error: {e}")
                    
        viz_thread = threading.Thread(target=visualization_loop, daemon=True)
        # Uncomment to start: viz_thread.start()
        
    return ml_logger, memory_visualizer