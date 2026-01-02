/**
 * Fast Screen Capture Streaming Implementation
 * Persistent ScreenCaptureKit streams for 60 FPS surveillance
 */

#include "fast_capture_stream.h"
#include <iostream>
#include <sstream>
#include <iomanip>
#include <deque>
#include <unordered_map>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <numeric>
#include <algorithm>
#include <memory>  // v29.0: For std::shared_ptr in DelegateSharedState
#include <cstdlib>  // v29.0: For getenv() in configurable shutdown timeout

#ifdef __APPLE__
#import <Foundation/Foundation.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreVideo/CoreVideo.h>
#import <CoreMedia/CoreMedia.h>
#import <Metal/Metal.h>
#import <ImageIO/ImageIO.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#include <dispatch/dispatch.h>

// ===== Streaming Delegate =====

/**
 * v29.0: ROBUST DELEGATE LIFECYCLE MANAGEMENT
 *
 * Fixes SIGABRT crash caused by:
 * 1. Race condition: Callbacks accessing dangling C++ pointers during shutdown
 * 2. Missing synchronization: Delegate destroyed while callbacks still pending
 * 3. No validity guard: No early-exit check before accessing shared state
 *
 * Solution:
 * - Atomic `stopping` flag for immediate callback rejection
 * - Atomic `activeCallbacks` counter to track in-flight callbacks
 * - Proper synchronization barrier before destroying shared state
 * - Comprehensive null/validity checks at every access point
 */

/**
 * Shared state container - outlives the delegate
 * This allows safe access even during shutdown transitions
 */
struct DelegateSharedState {
    std::atomic<bool> stopping{false};          // Signal to stop accepting frames
    std::atomic<int32_t> activeCallbacks{0};    // Count of in-flight callbacks
    std::atomic<bool> isValid{true};            // Master validity flag

    // Callback to wait for all active callbacks to drain
    void waitForCallbacksDrain(std::chrono::milliseconds timeout = std::chrono::milliseconds(2000)) {
        auto deadline = std::chrono::steady_clock::now() + timeout;
        while (activeCallbacks.load() > 0) {
            if (std::chrono::steady_clock::now() >= deadline) {
                // Timeout - force invalidate (callbacks will early-exit)
                isValid.store(false);
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
};

/**
 * Continuous streaming delegate for persistent SCStream
 * Receives frames at target FPS and buffers them
 *
 * v29.0: Now uses shared state for safe lifecycle management
 */
@interface JARVISStreamingDelegate : NSObject <SCStreamDelegate, SCStreamOutput>
// v29.0: Shared state for lifecycle management (shared_ptr so it outlives delegate)
@property (nonatomic, assign) std::shared_ptr<DelegateSharedState>* sharedState;

// Frame buffer and synchronization (guarded by sharedState validity)
@property (nonatomic, assign) std::queue<jarvis::vision::StreamFrame>* frameBuffer;
@property (nonatomic, assign) std::mutex* bufferMutex;
@property (nonatomic, assign) std::condition_variable* bufferCV;
@property (nonatomic, assign) size_t maxBufferSize;
@property (nonatomic, assign) bool dropOnOverflow;
@property (nonatomic, assign) std::atomic<uint64_t>* frameCounter;
@property (nonatomic, assign) std::atomic<uint64_t>* droppedCounter;
@property (nonatomic, assign) std::function<void(const jarvis::vision::StreamFrame&)>* frameCallback;
@property (nonatomic, assign) std::function<void(const std::string&)>* errorCallback;
@property (nonatomic, assign) std::string outputFormat;
@property (nonatomic, assign) int jpegQuality;
@property (nonatomic, assign) bool useGPU;
@property (nonatomic, assign) id<MTLDevice> metalDevice;
@end

@implementation JARVISStreamingDelegate

/**
 * v29.0: RAII guard for tracking active callbacks
 * Ensures activeCallbacks is decremented even on early return or exception
 */
struct CallbackGuard {
    std::shared_ptr<DelegateSharedState> state;
    bool valid;

    CallbackGuard(std::shared_ptr<DelegateSharedState>* statePtr)
        : state(statePtr ? *statePtr : nullptr), valid(false) {
        if (state && state->isValid.load() && !state->stopping.load()) {
            state->activeCallbacks.fetch_add(1);
            valid = true;
        }
    }

    ~CallbackGuard() {
        if (valid && state) {
            state->activeCallbacks.fetch_sub(1);
        }
    }

    bool isValid() const {
        return valid && state && state->isValid.load() && !state->stopping.load();
    }
};

- (void)stream:(SCStream *)stream
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
    ofType:(SCStreamOutputType)type {

    if (type != SCStreamOutputTypeScreen) return;

    // v29.0: CRITICAL - Guard against accessing dangling pointers during shutdown
    // This RAII guard tracks active callbacks and checks validity atomically
    CallbackGuard guard(_sharedState);
    if (!guard.isValid()) {
        return;  // Delegate is shutting down or invalid - early exit
    }

    @autoreleasepool {
        // v29.0: Double-check validity before any C++ object access
        if (!_sharedState || !(*_sharedState)->isValid.load()) {
            return;
        }

        // v29.0: Null-check all pointers before use
        if (!_frameBuffer || !_bufferMutex || !_bufferCV || !_frameCounter || !_droppedCounter) {
            return;  // Invalid state - cannot process
        }

        // Process frame
        jarvis::vision::StreamFrame frame = [self processSampleBuffer:sampleBuffer];

        if (frame.data.empty()) {
            return;  // Processing failed
        }

        // v29.0: Check validity again before modifying shared state
        if (!guard.isValid()) {
            return;
        }

        // Increment frame counter (atomic, safe even during shutdown)
        frame.frame_number = _frameCounter->fetch_add(1) + 1;

        // Call user callback if set
        if (_frameCallback && *_frameCallback) {
            try {
                (*_frameCallback)(frame);
            } catch (...) {
                // v29.0: Don't let callback exceptions crash the stream
            }
        }

        // v29.0: Final validity check before buffer modification
        if (!guard.isValid()) {
            return;
        }

        // Add to buffer
        {
            std::unique_lock<std::mutex> lock(*_bufferMutex);

            // v29.0: Check validity after acquiring lock (shutdown might have occurred while waiting)
            if (!guard.isValid()) {
                return;
            }

            // Check buffer capacity
            if (_maxBufferSize > 0 && _frameBuffer->size() >= _maxBufferSize) {
                if (_dropOnOverflow) {
                    // Drop oldest frame
                    _frameBuffer->pop();
                    _droppedCounter->fetch_add(1);
                } else {
                    // Wait for space (with periodic validity check)
                    size_t maxSize = _maxBufferSize;
                    while (_frameBuffer->size() >= maxSize) {
                        // v29.0: Use timed wait to check validity periodically
                        auto status = _bufferCV->wait_for(lock, std::chrono::milliseconds(100));
                        if (!guard.isValid()) {
                            return;  // Shutdown during wait
                        }
                        if (status == std::cv_status::timeout) {
                            // Check if we should continue waiting
                            if (!guard.isValid()) {
                                return;
                            }
                        }
                    }
                }
            }

            // v29.0: Final check before push
            if (guard.isValid()) {
                _frameBuffer->push(std::move(frame));
            }
        }

        // Notify waiting consumers (safe even if CV is being destroyed)
        if (guard.isValid() && _bufferCV) {
            try {
                _bufferCV->notify_one();
            } catch (...) {
                // v29.0: Ignore notification errors during shutdown
            }
        }
    }
}

- (jarvis::vision::StreamFrame)processSampleBuffer:(CMSampleBufferRef)sampleBuffer {
    jarvis::vision::StreamFrame frame;
    frame.timestamp = std::chrono::steady_clock::now();

    CVImageBufferRef imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (!imageBuffer) {
        if (_errorCallback && *_errorCallback) {
            (*_errorCallback)("No image buffer in sample");
        }
        return frame;
    }

    CVPixelBufferLockBaseAddress(imageBuffer, kCVPixelBufferLock_ReadOnly);

    size_t width = CVPixelBufferGetWidth(imageBuffer);
    size_t height = CVPixelBufferGetHeight(imageBuffer);
    size_t bytesPerRow = CVPixelBufferGetBytesPerRow(imageBuffer);
    void *baseAddress = CVPixelBufferGetBaseAddress(imageBuffer);

    frame.width = (int)width;
    frame.height = (int)height;
    frame.channels = 4;  // BGRA
    frame.format = _outputFormat;
    frame.gpu_accelerated = (_metalDevice != nil);

    if (_outputFormat == "raw") {
        // Zero-copy path: Store raw BGRA data
        size_t dataSize = bytesPerRow * height;
        frame.data.resize(dataSize);
        std::memcpy(frame.data.data(), baseAddress, dataSize);
        frame.memory_used = dataSize;
    } else {
        // Compression path: Convert to JPEG/PNG
        CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
        CGContextRef context = CGBitmapContextCreate(
            baseAddress, width, height, 8, bytesPerRow, colorSpace,
            kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little
        );

        CGImageRef cgImage = CGBitmapContextCreateImage(context);
        NSMutableData *imageData = [NSMutableData data];
        CGImageDestinationRef destination;

        if (_outputFormat == "jpeg") {
            destination = CGImageDestinationCreateWithData(
                (__bridge CFMutableDataRef)imageData,
                (__bridge CFStringRef)UTTypeJPEG.identifier,
                1, NULL
            );
            NSDictionary *properties = @{
                (__bridge NSString *)kCGImageDestinationLossyCompressionQuality: @(_jpegQuality / 100.0)
            };
            CGImageDestinationAddImage(destination, cgImage, (__bridge CFDictionaryRef)properties);
        } else {  // PNG
            destination = CGImageDestinationCreateWithData(
                (__bridge CFMutableDataRef)imageData,
                (__bridge CFStringRef)UTTypePNG.identifier,
                1, NULL
            );
            CGImageDestinationAddImage(destination, cgImage, NULL);
        }

        CGImageDestinationFinalize(destination);

        frame.data.resize(imageData.length);
        std::memcpy(frame.data.data(), imageData.bytes, imageData.length);
        frame.memory_used = imageData.length;

        CFRelease(destination);
        CGImageRelease(cgImage);
        CGContextRelease(context);
        CGColorSpaceRelease(colorSpace);
    }

    CVPixelBufferUnlockBaseAddress(imageBuffer, kCVPixelBufferLock_ReadOnly);

    // Calculate latency
    auto now = std::chrono::steady_clock::now();
    frame.capture_latency = std::chrono::duration_cast<std::chrono::microseconds>(now - frame.timestamp);

    return frame;
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    // v29.0: Check shared state validity before accessing callback
    if (!_sharedState || !(*_sharedState)->isValid.load() || (*_sharedState)->stopping.load()) {
        return;  // Shutting down - ignore error callback
    }

    if (error && _errorCallback && *_errorCallback) {
        try {
            (*_errorCallback)(std::string("Stream stopped: ") + error.localizedDescription.UTF8String);
        } catch (...) {
            // v29.0: Don't let callback exceptions crash the stream
        }
    }
}

@end

#endif

namespace jarvis {
namespace vision {

// ===== CaptureStream Implementation =====

class CaptureStream::Impl {
public:
    uint32_t window_id;
    StreamConfig config;
    std::atomic<bool> active{false};

#ifdef __APPLE__
    SCStream *stream = nil;
    JARVISStreamingDelegate *delegate = nil;
    SCShareableContent *shareable_content = nil;
    dispatch_queue_t capture_queue = nil;
    id<MTLDevice> metal_device = nil;
#endif

    // v29.0: Shared state for safe delegate lifecycle management
    std::shared_ptr<DelegateSharedState> shared_state;

    // Frame buffer
    std::queue<StreamFrame> frame_buffer;
    mutable std::mutex buffer_mutex;
    std::condition_variable buffer_cv;

    // Statistics
    std::atomic<uint64_t> frame_counter{0};
    std::atomic<uint64_t> dropped_counter{0};
    std::chrono::steady_clock::time_point stream_start_time;
    std::deque<std::chrono::microseconds> latency_samples;
    static constexpr size_t MAX_LATENCY_SAMPLES = 1000;
    mutable std::mutex stats_mutex;
    size_t peak_buffer_size = 0;
    uint64_t total_bytes = 0;

    // Callbacks
    std::function<void(const StreamFrame&)> frame_callback;
    std::function<void(const std::string&)> error_callback;

    Impl(uint32_t wid, const StreamConfig& cfg)
        : window_id(wid), config(cfg), frame_callback(cfg.frame_callback),
          error_callback(cfg.error_callback),
          shared_state(std::make_shared<DelegateSharedState>()) {  // v29.0: Initialize shared state
#ifdef __APPLE__
        // Initialize Metal if GPU acceleration enabled
        if (config.use_gpu_acceleration) {
            metal_device = MTLCreateSystemDefaultDevice();
        }

        // Create capture queue
        dispatch_queue_attr_t attr = dispatch_queue_attr_make_with_qos_class(
            DISPATCH_QUEUE_SERIAL,
            QOS_CLASS_USER_INTERACTIVE,
            -1
        );
        capture_queue = dispatch_queue_create("com.jarvis.stream.capture", attr);

        // Fetch shareable content
        refresh_content();
#endif
    }

    ~Impl() {
        stop_stream();
#ifdef __APPLE__
        // v29.0: Ensure capture queue is fully drained before releasing
        if (capture_queue) {
            // Synchronous barrier to ensure all pending work completes
            dispatch_sync(capture_queue, ^{
                // Empty block - just drains the queue
            });
            dispatch_release(capture_queue);
            capture_queue = nil;
        }

        // v29.0: Final invalidation of shared state (callbacks will early-exit)
        if (shared_state) {
            shared_state->isValid.store(false);
        }
#endif
    }

#ifdef __APPLE__
    void refresh_content() {
        __block SCShareableContent *fetchedContent = nil;
        dispatch_semaphore_t sem = dispatch_semaphore_create(0);

        [SCShareableContent getShareableContentWithCompletionHandler:^(SCShareableContent * _Nullable content, NSError * _Nullable error) {
            if (content) {
                fetchedContent = [content retain];
            } else if (error && error_callback) {
                error_callback("Failed to get shareable content: " + std::string(error.localizedDescription.UTF8String));
            }
            dispatch_semaphore_signal(sem);
        }];

        dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 1 * NSEC_PER_SEC));
        shareable_content = fetchedContent;
    }

    SCWindow* find_window() {
        if (!shareable_content) return nil;

        for (SCWindow *window in shareable_content.windows) {
            if ((uint32_t)window.windowID == window_id) {
                return window;
            }
        }
        return nil;
    }
#endif

    bool start_stream() {
        if (active.load()) {
            return true;  // Already active
        }

#ifdef __APPLE__
        @autoreleasepool {
            SCWindow *target_window = find_window();
            if (!target_window) {
                refresh_content();
                target_window = find_window();
            }

            if (!target_window) {
                if (error_callback) {
                    error_callback("Window not found: " + std::to_string(window_id));
                }
                return false;
            }

            // Create content filter
            SCContentFilter *filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:target_window];

            // Configure stream
            SCStreamConfiguration *streamConfig = [[SCStreamConfiguration alloc] init];

            // Apply resolution scaling
            int scaled_width = (int)(target_window.frame.size.width * config.resolution_scale);
            int scaled_height = (int)(target_window.frame.size.height * config.resolution_scale);

            streamConfig.width = scaled_width;
            streamConfig.height = scaled_height;
            streamConfig.minimumFrameInterval = CMTimeMake(1, config.target_fps);
            streamConfig.queueDepth = 3;  // Small buffer for low latency
            streamConfig.showsCursor = config.capture_cursor;
            streamConfig.pixelFormat = kCVPixelFormatType_32BGRA;

            if (@available(macOS 14.0, *)) {
                if (config.use_gpu_acceleration && metal_device) {
                    streamConfig.captureResolution = SCCaptureResolutionAutomatic;
                }
            }

            // v29.0: Reset shared state for new stream (in case of restart)
            shared_state->stopping.store(false);
            shared_state->isValid.store(true);
            shared_state->activeCallbacks.store(0);

            // Create delegate
            delegate = [[JARVISStreamingDelegate alloc] init];
            delegate.sharedState = &shared_state;  // v29.0: Connect shared state
            delegate.frameBuffer = &frame_buffer;
            delegate.bufferMutex = &buffer_mutex;
            delegate.bufferCV = &buffer_cv;
            delegate.maxBufferSize = config.max_buffer_size;
            delegate.dropOnOverflow = config.drop_frames_on_overflow;
            delegate.frameCounter = &frame_counter;
            delegate.droppedCounter = &dropped_counter;
            delegate.frameCallback = &frame_callback;
            delegate.errorCallback = &error_callback;
            delegate.outputFormat = config.output_format;
            delegate.jpegQuality = config.jpeg_quality;
            delegate.useGPU = config.use_gpu_acceleration;
            delegate.metalDevice = metal_device;

            // Create stream
            NSError *error = nil;
            stream = [[SCStream alloc] initWithFilter:filter
                                        configuration:streamConfig
                                             delegate:delegate];

            if (!stream) {
                if (error_callback) {
                    error_callback("Failed to create stream");
                }
                return false;
            }

            // Add stream output
            [stream addStreamOutput:delegate
                               type:SCStreamOutputTypeScreen
                  sampleHandlerQueue:capture_queue
                              error:&error];

            if (error) {
                if (error_callback) {
                    error_callback(std::string("Failed to add stream output: ") + error.localizedDescription.UTF8String);
                }
                return false;
            }

            // Start capture
            __block bool started = false;
            dispatch_semaphore_t start_sem = dispatch_semaphore_create(0);

            [stream startCaptureWithCompletionHandler:^(NSError * _Nullable error) {
                if (!error) {
                    started = true;
                } else if (error_callback) {
                    error_callback("Stream start failed: " + std::string(error.localizedDescription.UTF8String));
                }
                dispatch_semaphore_signal(start_sem);
            }];

            dispatch_semaphore_wait(start_sem, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));

            if (started) {
                active.store(true);
                stream_start_time = std::chrono::steady_clock::now();
                return true;
            }

            return false;
        }
#else
        if (error_callback) {
            error_callback("ScreenCaptureKit not available on this platform");
        }
        return false;
#endif
    }

    /**
     * v29.0: ROBUST STREAM SHUTDOWN PROTOCOL
     *
     * The shutdown sequence is carefully ordered to prevent SIGABRT:
     *
     * 1. Signal stopping via shared_state (callbacks will early-exit)
     * 2. Set active=false to stop external consumers
     * 3. Wake up any waiting consumers (they'll see active=false)
     * 4. Wait for all active callbacks to drain
     * 5. Stop the SCStream capture
     * 6. Release delegate (safe now - no callbacks in flight)
     * 7. Clear the buffer
     * 8. Invalidate shared state (final safety)
     */
    void stop_stream() {
        if (!active.load()) {
            return;
        }

        // v29.0: PHASE 1 - Signal shutdown to delegate callbacks FIRST
        // This ensures any in-flight or pending callbacks will early-exit
        if (shared_state) {
            shared_state->stopping.store(true);
        }

        // v29.0: PHASE 2 - Signal to consumers that stream is stopping
        active.store(false);

        // v29.0: PHASE 3 - Wake up any waiting consumers
        // They'll check active flag and return
        buffer_cv.notify_all();

#ifdef __APPLE__
        // v29.0: PHASE 4 - Wait for active callbacks to drain
        // This is CRITICAL - ensures no callbacks are accessing C++ objects
        if (shared_state) {
            // Configurable timeout from environment
            const char* timeout_env = getenv("JARVIS_STREAM_SHUTDOWN_TIMEOUT_MS");
            int timeout_ms = timeout_env ? atoi(timeout_env) : 2000;

            shared_state->waitForCallbacksDrain(std::chrono::milliseconds(timeout_ms));
        }

        // v29.0: PHASE 5 - Stop the SCStream
        if (stream) {
            __block bool stopped = false;
            dispatch_semaphore_t stop_sem = dispatch_semaphore_create(0);

            [stream stopCaptureWithCompletionHandler:^(NSError * _Nullable error) {
                stopped = true;
                dispatch_semaphore_signal(stop_sem);
            }];

            // Wait with timeout
            dispatch_semaphore_wait(stop_sem, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));

            // v29.0: PHASE 6 - Drain capture queue before releasing delegate
            // This ensures any final callbacks complete before delegate is nil'd
            if (capture_queue) {
                dispatch_sync(capture_queue, ^{
                    // Barrier block - ensures queue is drained
                });
            }

            // v29.0: Now safe to release - no callbacks can be in flight
            stream = nil;
            delegate = nil;
        }
#endif

        // v29.0: PHASE 7 - Clear buffer (safe now - no callbacks accessing it)
        {
            std::lock_guard<std::mutex> lock(buffer_mutex);
            while (!frame_buffer.empty()) {
                frame_buffer.pop();
            }
        }

        // v29.0: PHASE 8 - Final invalidation (extra safety)
        if (shared_state) {
            shared_state->isValid.store(false);
        }

        // Final notification for any remaining waiters
        buffer_cv.notify_all();
    }

    void record_latency(std::chrono::microseconds latency) {
        std::lock_guard<std::mutex> lock(stats_mutex);
        latency_samples.push_back(latency);
        if (latency_samples.size() > MAX_LATENCY_SAMPLES) {
            latency_samples.pop_front();
        }
    }

    StreamStats get_statistics() const {
        StreamStats stats;
        stats.total_frames = frame_counter.load();
        stats.dropped_frames = dropped_counter.load();
        stats.is_active = active.load();
        stats.stream_start_time = stream_start_time;

        {
            std::lock_guard<std::mutex> lock(buffer_mutex);
            stats.current_buffer_size = frame_buffer.size();
        }

        {
            std::lock_guard<std::mutex> lock(stats_mutex);
            stats.peak_buffer_size = peak_buffer_size;
            stats.bytes_processed = total_bytes;

            if (!latency_samples.empty()) {
                auto sum = std::accumulate(latency_samples.begin(), latency_samples.end(),
                                          std::chrono::microseconds(0));
                stats.avg_latency_ms = sum.count() / (double)latency_samples.size() / 1000.0;

                auto minmax = std::minmax_element(latency_samples.begin(), latency_samples.end());
                stats.min_latency_ms = minmax.first->count() / 1000.0;
                stats.max_latency_ms = minmax.second->count() / 1000.0;
            }
        }

        // Calculate FPS
        auto uptime = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - stream_start_time
        );
        if (uptime.count() > 0) {
            stats.actual_fps = stats.total_frames / (double)uptime.count();
        }

        return stats;
    }
};

// ===== CaptureStream Public API =====

CaptureStream::CaptureStream(uint32_t window_id, const StreamConfig& config)
    : pImpl(std::make_unique<Impl>(window_id, config)) {}

CaptureStream::~CaptureStream() = default;
CaptureStream::CaptureStream(CaptureStream&&) noexcept = default;
CaptureStream& CaptureStream::operator=(CaptureStream&&) noexcept = default;

bool CaptureStream::start() {
    return pImpl->start_stream();
}

void CaptureStream::stop() {
    pImpl->stop_stream();
}

bool CaptureStream::is_active() const {
    return pImpl->active.load();
}

std::unique_ptr<StreamFrame> CaptureStream::get_frame(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(pImpl->buffer_mutex);

    if (pImpl->buffer_cv.wait_for(lock, timeout, [this] { return !pImpl->frame_buffer.empty(); })) {
        auto frame = std::make_unique<StreamFrame>(std::move(pImpl->frame_buffer.front()));
        pImpl->frame_buffer.pop();
        pImpl->record_latency(frame->capture_latency);
        return frame;
    }

    return nullptr;  // Timeout
}

std::unique_ptr<StreamFrame> CaptureStream::try_get_frame() {
    std::lock_guard<std::mutex> lock(pImpl->buffer_mutex);

    if (!pImpl->frame_buffer.empty()) {
        auto frame = std::make_unique<StreamFrame>(std::move(pImpl->frame_buffer.front()));
        pImpl->frame_buffer.pop();
        pImpl->record_latency(frame->capture_latency);
        return frame;
    }

    return nullptr;
}

const StreamFrame* CaptureStream::peek_latest() const {
    std::lock_guard<std::mutex> lock(pImpl->buffer_mutex);
    return pImpl->frame_buffer.empty() ? nullptr : &pImpl->frame_buffer.back();
}

std::vector<StreamFrame> CaptureStream::get_all_frames() {
    std::lock_guard<std::mutex> lock(pImpl->buffer_mutex);
    std::vector<StreamFrame> frames;

    while (!pImpl->frame_buffer.empty()) {
        frames.push_back(std::move(pImpl->frame_buffer.front()));
        pImpl->frame_buffer.pop();
    }

    return frames;
}

StreamStats CaptureStream::get_stats() const {
    return pImpl->get_statistics();
}

void CaptureStream::reset_stats() {
    pImpl->frame_counter.store(0);
    pImpl->dropped_counter.store(0);
    std::lock_guard<std::mutex> lock(pImpl->stats_mutex);
    pImpl->latency_samples.clear();
    pImpl->peak_buffer_size = 0;
    pImpl->total_bytes = 0;
}

void CaptureStream::update_config(const StreamConfig& config) {
    bool was_active = is_active();
    if (was_active) {
        stop();
    }

    pImpl->config = config;

    if (was_active) {
        start();
    }
}

StreamConfig CaptureStream::get_config() const {
    return pImpl->config;
}

uint32_t CaptureStream::get_window_id() const {
    return pImpl->window_id;
}

WindowInfo CaptureStream::get_window_info() const {
    WindowInfo info;
#ifdef __APPLE__
    SCWindow *window = pImpl->find_window();
    if (window) {
        info.window_id = (uint32_t)window.windowID;
        info.app_name = window.owningApplication.applicationName.UTF8String ?: "";
        info.window_title = window.title.UTF8String ?: "";
        info.bundle_identifier = window.owningApplication.bundleIdentifier.UTF8String ?: "";
        info.x = (int)window.frame.origin.x;
        info.y = (int)window.frame.origin.y;
        info.width = (int)window.frame.size.width;
        info.height = (int)window.frame.size.height;
        info.is_visible = window.isOnScreen;
    }
#endif
    return info;
}

// ===== StreamManager Implementation =====

class StreamManager::Impl {
public:
    std::unordered_map<std::string, std::unique_ptr<CaptureStream>> streams;
    mutable std::mutex streams_mutex;
    size_t max_concurrent_streams = 10;
    std::atomic<uint64_t> stream_id_counter{0};

    std::string generate_stream_id() {
        uint64_t id = ++stream_id_counter;
        std::stringstream ss;
        ss << "stream_" << std::setfill('0') << std::setw(8) << id;
        return ss.str();
    }
};

StreamManager::StreamManager() : pImpl(std::make_unique<Impl>()) {}
StreamManager::~StreamManager() = default;

std::string StreamManager::create_stream(uint32_t window_id, const StreamConfig& config) {
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);

    if (pImpl->streams.size() >= pImpl->max_concurrent_streams) {
        throw std::runtime_error("Maximum concurrent streams reached");
    }

    std::string stream_id = pImpl->generate_stream_id();
    auto stream = std::make_unique<CaptureStream>(window_id, config);

    if (!stream->start()) {
        throw std::runtime_error("Failed to start stream for window " + std::to_string(window_id));
    }

    pImpl->streams[stream_id] = std::move(stream);
    return stream_id;
}

std::string StreamManager::create_stream_by_name(const std::string& app_name,
                                                const std::string& window_title,
                                                const StreamConfig& config) {
#ifdef __APPLE__
    // Find window using ScreenCaptureKit directly
    __block SCShareableContent *content = nil;
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);

    [SCShareableContent getShareableContentWithCompletionHandler:^(SCShareableContent * _Nullable shareableContent, NSError * _Nullable error) {
        if (shareableContent) {
            content = [shareableContent retain];
        }
        dispatch_semaphore_signal(sem);
    }];

    dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));

    if (!content) {
        throw std::runtime_error("Failed to get shareable content");
    }

    // Search for window
    SCWindow *target_window = nil;
    for (SCWindow *window in content.windows) {
        NSString *appNameNS = window.owningApplication.applicationName;
        NSString *titleNS = window.title;

        bool appMatches = appNameNS && [appNameNS.lowercaseString containsString:@(app_name.c_str())];
        bool titleMatches = window_title.empty() ||
                          (titleNS && [titleNS.lowercaseString containsString:@(window_title.c_str())]);

        if (appMatches && titleMatches) {
            target_window = window;
            break;
        }
    }

    [content release];

    if (!target_window) {
        throw std::runtime_error("Window not found: " + app_name);
    }

    return create_stream((uint32_t)target_window.windowID, config);
#else
    throw std::runtime_error("ScreenCaptureKit not available on this platform");
#endif
}

void StreamManager::destroy_stream(const std::string& stream_id) {
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);
    pImpl->streams.erase(stream_id);
}

void StreamManager::destroy_all_streams() {
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);
    pImpl->streams.clear();
}

std::unique_ptr<StreamFrame> StreamManager::get_frame(const std::string& stream_id,
                                                      std::chrono::milliseconds timeout) {
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);
    auto it = pImpl->streams.find(stream_id);
    if (it != pImpl->streams.end()) {
        return it->second->get_frame(timeout);
    }
    return nullptr;
}

std::unordered_map<std::string, StreamFrame> StreamManager::get_all_frames(
    std::chrono::milliseconds timeout) {
    std::unordered_map<std::string, StreamFrame> frames;
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);

    for (auto& [id, stream] : pImpl->streams) {
        auto frame = stream->get_frame(timeout);
        if (frame) {
            frames[id] = std::move(*frame);
        }
    }

    return frames;
}

std::vector<std::string> StreamManager::get_active_stream_ids() const {
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);
    std::vector<std::string> ids;
    for (const auto& [id, stream] : pImpl->streams) {
        if (stream->is_active()) {
            ids.push_back(id);
        }
    }
    return ids;
}

StreamStats StreamManager::get_stream_stats(const std::string& stream_id) const {
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);
    auto it = pImpl->streams.find(stream_id);
    if (it != pImpl->streams.end()) {
        return it->second->get_stats();
    }
    return StreamStats{};
}

std::unordered_map<std::string, StreamStats> StreamManager::get_all_stats() const {
    std::unordered_map<std::string, StreamStats> all_stats;
    std::lock_guard<std::mutex> lock(pImpl->streams_mutex);

    for (const auto& [id, stream] : pImpl->streams) {
        all_stats[id] = stream->get_stats();
    }

    return all_stats;
}

size_t StreamManager::get_active_stream_count() const {
    return get_active_stream_ids().size();
}

size_t StreamManager::get_total_memory_usage() const {
    size_t total = 0;
    auto stats = get_all_stats();
    for (const auto& [id, stat] : stats) {
        total += stat.bytes_processed;
    }
    return total;
}

void StreamManager::set_max_concurrent_streams(size_t max) {
    pImpl->max_concurrent_streams = max;
}

// ===== Utility Functions =====

bool is_screencapturekit_available() {
#ifdef __APPLE__
    if (@available(macOS 12.3, *)) {
        return true;
    }
#endif
    return false;
}

int get_recommended_fps(int width, int height, bool gpu_available) {
    size_t pixel_count = width * height;

    if (pixel_count > 1920 * 1080) {
        // 4K or larger: 30 FPS max
        return gpu_available ? 30 : 15;
    } else if (pixel_count > 1280 * 720) {
        // 1080p: 60 FPS with GPU, 30 without
        return gpu_available ? 60 : 30;
    } else {
        // 720p or smaller: 60 FPS
        return 60;
    }
}

size_t estimate_stream_memory(const StreamConfig& config, int width, int height) {
    size_t frame_size;

    if (config.output_format == "raw") {
        frame_size = width * height * 4;  // BGRA
    } else if (config.output_format == "jpeg") {
        frame_size = (width * height * 4) / 10;  // ~10:1 compression
    } else {  // PNG
        frame_size = (width * height * 4) / 3;   // ~3:1 compression
    }

    return frame_size * config.max_buffer_size;
}

} // namespace vision
} // namespace jarvis
