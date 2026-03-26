/**
 * Zero-Copy Shared Memory Frame Bridge
 *
 * Eliminates the pybind11 GIL contention bottleneck that caps SCK at 1.2fps
 * through Python. The C++ capture daemon writes raw BGRA pixels into a POSIX
 * shared memory segment. Python reads them via numpy.frombuffer() -- zero copy,
 * zero GIL, zero function calls across the language boundary.
 *
 * Memory Layout (double-buffered):
 *   [Header: 64 bytes]
 *     uint64_t frame_counter    -- monotonic, incremented by writer on each frame
 *     uint32_t width            -- frame width in pixels
 *     uint32_t height           -- frame height in pixels
 *     uint32_t channels         -- always 4 (BGRA)
 *     uint32_t active_buffer    -- 0 or 1 (which buffer has the latest frame)
 *     uint64_t timestamp_ns     -- capture timestamp (steady_clock nanoseconds)
 *     uint32_t writer_pid       -- PID of the writing process
 *     uint8_t  padding[20]      -- alignment to 64 bytes
 *
 *   [Buffer 0: width * height * channels bytes]
 *   [Buffer 1: width * height * channels bytes]
 *
 * Double-buffering: writer fills the INACTIVE buffer, then atomically flips
 * active_buffer. Reader always reads the ACTIVE buffer. No locks needed --
 * the atomic flip is the synchronization primitive.
 *
 * Boundary Mandate: this is pure deterministic infrastructure. No intelligence,
 * no decisions, no agentic behavior. Just memory physics.
 */

#pragma once

#include <cstdint>
#include <cstring>
#include <string>
#include <atomic>

#ifdef __APPLE__
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#endif

namespace jarvis { namespace vision { namespace shm {

// Shared memory segment name
static constexpr const char* SHM_NAME = "/jarvis_frame_bridge";

// Header layout -- exactly 64 bytes, naturally aligned
struct FrameHeader {
    std::atomic<uint64_t> frame_counter;   // 8 bytes
    uint32_t width;                         // 4 bytes
    uint32_t height;                        // 4 bytes
    uint32_t channels;                      // 4 bytes
    std::atomic<uint32_t> active_buffer;   // 4 bytes (0 or 1)
    uint64_t timestamp_ns;                  // 8 bytes
    uint32_t writer_pid;                    // 4 bytes
    uint32_t frame_size;                    // 4 bytes (width * height * channels)
    uint8_t  padding[24];                   // pad to 64 bytes
};

static_assert(sizeof(FrameHeader) == 64, "Header must be exactly 64 bytes");

/**
 * Writer side: C++ SCK daemon writes frames here.
 * Called from the SCK delegate's didOutputSampleBuffer callback.
 */
class ShmFrameWriter {
public:
    ShmFrameWriter() : shm_fd_(-1), shm_ptr_(nullptr), shm_size_(0) {}

    ~ShmFrameWriter() {
        close();
    }

    /**
     * Create and map the shared memory segment.
     * Returns true on success. Idempotent -- safe to call multiple times.
     */
    bool open(uint32_t width, uint32_t height, uint32_t channels = 4) {
        if (shm_ptr_) return true;  // Already open

#ifdef __APPLE__
        uint32_t frame_size = width * height * channels;
        shm_size_ = sizeof(FrameHeader) + (frame_size * 2);  // double buffer

        // Create shared memory
        shm_fd_ = shm_open(SHM_NAME, O_CREAT | O_RDWR, 0666);
        if (shm_fd_ < 0) return false;

        // Size it
        if (ftruncate(shm_fd_, shm_size_) < 0) {
            ::close(shm_fd_);
            shm_fd_ = -1;
            return false;
        }

        // Map it
        shm_ptr_ = mmap(nullptr, shm_size_, PROT_READ | PROT_WRITE,
                         MAP_SHARED, shm_fd_, 0);
        if (shm_ptr_ == MAP_FAILED) {
            shm_ptr_ = nullptr;
            ::close(shm_fd_);
            shm_fd_ = -1;
            return false;
        }

        // Initialize header
        auto* header = reinterpret_cast<FrameHeader*>(shm_ptr_);
        header->frame_counter.store(0, std::memory_order_relaxed);
        header->width = width;
        header->height = height;
        header->channels = channels;
        header->active_buffer.store(0, std::memory_order_relaxed);
        header->timestamp_ns = 0;
        header->writer_pid = getpid();
        header->frame_size = frame_size;
        memset(header->padding, 0, sizeof(header->padding));

        return true;
#else
        return false;
#endif
    }

    /**
     * Write a frame to the INACTIVE buffer, then flip.
     * Called from the SCK delegate on the capture_queue thread.
     * ~0.5ms for a 1440x900x4 frame (5.2MB memcpy).
     */
    void write_frame(const uint8_t* pixels, uint32_t size, uint64_t timestamp_ns) {
        if (!shm_ptr_) return;

        auto* header = reinterpret_cast<FrameHeader*>(shm_ptr_);
        uint32_t frame_size = header->frame_size;
        if (size < frame_size) return;  // Frame too small

        // Write to INACTIVE buffer
        uint32_t active = header->active_buffer.load(std::memory_order_acquire);
        uint32_t write_buf = 1 - active;  // The other buffer

        uint8_t* dst = static_cast<uint8_t*>(shm_ptr_)
                       + sizeof(FrameHeader)
                       + (write_buf * frame_size);

        memcpy(dst, pixels, frame_size);

        // Update metadata
        header->timestamp_ns = timestamp_ns;

        // Atomic flip -- this is the ONLY synchronization point
        header->active_buffer.store(write_buf, std::memory_order_release);
        header->frame_counter.fetch_add(1, std::memory_order_relaxed);
    }

    void close() {
#ifdef __APPLE__
        if (shm_ptr_) {
            munmap(shm_ptr_, shm_size_);
            shm_ptr_ = nullptr;
        }
        if (shm_fd_ >= 0) {
            ::close(shm_fd_);
            shm_unlink(SHM_NAME);
            shm_fd_ = -1;
        }
#endif
    }

    bool is_open() const { return shm_ptr_ != nullptr; }

private:
    int shm_fd_;
    void* shm_ptr_;
    size_t shm_size_;
};

}}} // namespace jarvis::vision::shm
