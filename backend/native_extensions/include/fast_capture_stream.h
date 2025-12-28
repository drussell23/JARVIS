/**
 * Fast Screen Capture Streaming Engine for JARVIS Vision System
 * Persistent ScreenCaptureKit streams for continuous 60 FPS surveillance
 *
 * Design Principles:
 * - Persistent streaming: Keep SCStream alive for continuous capture
 * - Thread-safe frame buffering: Lock-free queues for max throughput
 * - Async/await friendly: Python asyncio compatible
 * - Zero-copy when possible: Direct buffer access
 * - Dynamic configuration: No hardcoded values
 * - Resource efficient: Automatic cleanup and memory management
 */

#ifndef JARVIS_FAST_CAPTURE_STREAM_H
#define JARVIS_FAST_CAPTURE_STREAM_H

#include "fast_capture.h"
#include <queue>
#include <atomic>
#include <condition_variable>

namespace jarvis {
namespace vision {

/**
 * Frame from continuous stream
 */
struct StreamFrame {
    std::vector<uint8_t> data;  // Raw BGRA or compressed data
    int width = 0;
    int height = 0;
    int channels = 4;  // BGRA
    std::string format = "raw";  // "raw", "jpeg", "png"

    uint64_t frame_number = 0;
    std::chrono::steady_clock::time_point timestamp;
    std::chrono::microseconds capture_latency{0};

    bool gpu_accelerated = false;
    size_t memory_used = 0;
};

/**
 * Stream configuration
 */
struct StreamConfig {
    // Target FPS (1-60)
    int target_fps = 30;

    // Frame buffer size (0 = unbounded, careful with memory!)
    size_t max_buffer_size = 10;  // Keep last 10 frames

    // Output format
    std::string output_format = "raw";  // "raw" for zero-copy, "jpeg"/"png" for compression
    int jpeg_quality = 85;

    // Performance
    bool use_gpu_acceleration = true;
    bool drop_frames_on_overflow = true;  // Drop oldest frames if buffer full

    // Capture options
    bool capture_cursor = false;
    bool capture_shadow = false;

    // Resolution scaling (1.0 = native, 0.5 = half, 2.0 = retina)
    float resolution_scale = 1.0;

    // Callbacks
    std::function<void(const StreamFrame&)> frame_callback;  // Called on each frame
    std::function<void(const std::string&)> error_callback;
};

/**
 * Stream statistics
 */
struct StreamStats {
    uint64_t total_frames = 0;
    uint64_t dropped_frames = 0;
    double actual_fps = 0.0;
    double avg_latency_ms = 0.0;
    double min_latency_ms = 0.0;
    double max_latency_ms = 0.0;
    size_t current_buffer_size = 0;
    size_t peak_buffer_size = 0;
    uint64_t bytes_processed = 0;
    std::chrono::steady_clock::time_point stream_start_time;
    bool is_active = false;
};

/**
 * Continuous capture stream for a single window
 * Manages persistent SCStream with frame buffering
 */
class CaptureStream {
public:
    CaptureStream(uint32_t window_id, const StreamConfig& config = {});
    ~CaptureStream();

    // Disable copying
    CaptureStream(const CaptureStream&) = delete;
    CaptureStream& operator=(const CaptureStream&) = delete;

    // Allow moving
    CaptureStream(CaptureStream&&) noexcept;
    CaptureStream& operator=(CaptureStream&&) noexcept;

    // ===== Stream Control =====
    bool start();
    void stop();
    bool is_active() const;

    // ===== Frame Access =====

    /**
     * Get latest frame (blocking with timeout)
     * Returns nullptr if no frame available within timeout
     */
    std::unique_ptr<StreamFrame> get_frame(std::chrono::milliseconds timeout = std::chrono::milliseconds(100));

    /**
     * Get latest frame (non-blocking)
     * Returns nullptr if no frame available
     */
    std::unique_ptr<StreamFrame> try_get_frame();

    /**
     * Peek at latest frame without removing from buffer
     */
    const StreamFrame* peek_latest() const;

    /**
     * Get all available frames (drains buffer)
     */
    std::vector<StreamFrame> get_all_frames();

    // ===== Statistics =====
    StreamStats get_stats() const;
    void reset_stats();

    // ===== Configuration =====
    void update_config(const StreamConfig& config);
    StreamConfig get_config() const;

    // ===== Window Info =====
    uint32_t get_window_id() const;
    WindowInfo get_window_info() const;

private:
    class Impl;
    std::unique_ptr<Impl> pImpl;
};

/**
 * Stream manager for multiple concurrent streams
 * Manages multiple windows simultaneously (God Mode)
 */
class StreamManager {
public:
    StreamManager();
    ~StreamManager();

    // ===== Stream Management =====

    /**
     * Create and start a new stream
     * Returns stream ID (unique identifier)
     */
    std::string create_stream(uint32_t window_id, const StreamConfig& config = {});

    /**
     * Create stream from window name
     */
    std::string create_stream_by_name(const std::string& app_name,
                                     const std::string& window_title = "",
                                     const StreamConfig& config = {});

    /**
     * Stop and destroy a stream
     */
    void destroy_stream(const std::string& stream_id);

    /**
     * Stop all streams
     */
    void destroy_all_streams();

    // ===== Frame Access =====

    /**
     * Get frame from specific stream
     */
    std::unique_ptr<StreamFrame> get_frame(const std::string& stream_id,
                                          std::chrono::milliseconds timeout = std::chrono::milliseconds(100));

    /**
     * Get frames from all active streams
     * Returns map of stream_id -> frame
     */
    std::unordered_map<std::string, StreamFrame> get_all_frames(
        std::chrono::milliseconds timeout = std::chrono::milliseconds(100));

    // ===== Stream Info =====
    std::vector<std::string> get_active_stream_ids() const;
    StreamStats get_stream_stats(const std::string& stream_id) const;
    std::unordered_map<std::string, StreamStats> get_all_stats() const;

    // ===== Resource Management =====
    size_t get_active_stream_count() const;
    size_t get_total_memory_usage() const;
    void set_max_concurrent_streams(size_t max);

private:
    class Impl;
    std::unique_ptr<Impl> pImpl;
};

// ===== Utility Functions =====

/**
 * Check if ScreenCaptureKit is available
 * Requires macOS 12.3+
 */
bool is_screencapturekit_available();

/**
 * Get recommended FPS based on window size and system capabilities
 */
int get_recommended_fps(int width, int height, bool gpu_available = true);

/**
 * Estimate memory usage for stream configuration
 */
size_t estimate_stream_memory(const StreamConfig& config, int width, int height);

} // namespace vision
} // namespace jarvis

#endif // JARVIS_FAST_CAPTURE_STREAM_H
