/**
 * Zero-Copy Shared Memory Ring Buffer Frame Bridge
 *
 * 5-slot ring buffer absorbs SCK burst delivery. C++ writes at 60fps in
 * bursts. Python reads the latest slot via numpy.frombuffer() -- zero copy,
 * zero GIL, zero function calls across the language boundary.
 *
 * Memory Layout:
 *   [Header: 128 bytes]
 *     uint64_t frame_counter      -- monotonic counter (total frames written)
 *     uint32_t width              -- frame width in pixels
 *     uint32_t height             -- frame height in pixels
 *     uint32_t channels           -- 4 (BGRA)
 *     uint32_t ring_size          -- number of slots (5)
 *     uint32_t write_index        -- next slot C++ will write to (0..ring_size-1)
 *     uint32_t latest_index       -- slot with the most recent complete frame
 *     uint32_t frame_size         -- width * height * channels
 *     uint32_t writer_pid         -- PID of the writer process
 *     uint64_t timestamps[5]      -- per-slot capture timestamp (nanoseconds)
 *     uint8_t  padding[...]       -- align to 128 bytes
 *
 *   [Slot 0: frame_size bytes]
 *   [Slot 1: frame_size bytes]
 *   [Slot 2: frame_size bytes]
 *   [Slot 3: frame_size bytes]
 *   [Slot 4: frame_size bytes]
 *
 * Protocol:
 *   Writer: memcpy pixels into slot[write_index], set timestamps[write_index],
 *           atomically update latest_index = write_index, increment frame_counter,
 *           advance write_index = (write_index + 1) % ring_size.
 *   Reader: read latest_index, read from slot[latest_index] via frombuffer.
 *           Writer never overwrites latest_index until the NEXT write completes.
 *           With 5 slots, the writer has 4 slots of headroom before wrapping.
 */

#pragma once

#include <cstdint>
#include <cstring>
#include <atomic>

#ifdef __APPLE__
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#endif

namespace jarvis { namespace vision { namespace shm {

static constexpr const char* SHM_NAME = "/jarvis_frame_bridge";
static constexpr uint32_t RING_SIZE = 5;

// Header: 128 bytes
struct RingHeader {
    std::atomic<uint64_t> frame_counter;    // 8
    uint32_t width;                          // 4
    uint32_t height;                         // 4
    uint32_t channels;                       // 4
    uint32_t ring_size;                      // 4
    std::atomic<uint32_t> write_index;      // 4
    std::atomic<uint32_t> latest_index;     // 4
    uint32_t frame_size;                     // 4
    uint32_t writer_pid;                     // 4
    uint64_t timestamps[RING_SIZE];          // 40 (5 * 8)
    uint8_t  padding[48];                    // 48 to reach 128
};

static_assert(sizeof(RingHeader) == 128, "RingHeader must be 128 bytes");

class ShmFrameWriter {
public:
    ShmFrameWriter() : shm_fd_(-1), shm_ptr_(nullptr), shm_size_(0) {}
    ~ShmFrameWriter() { close(); }

    bool open(uint32_t width, uint32_t height, uint32_t channels = 4) {
        if (shm_ptr_) return true;
#ifdef __APPLE__
        uint32_t frame_size = width * height * channels;
        shm_size_ = sizeof(RingHeader) + (frame_size * RING_SIZE);

        shm_fd_ = shm_open(SHM_NAME, O_CREAT | O_RDWR, 0666);
        if (shm_fd_ < 0) return false;

        if (ftruncate(shm_fd_, shm_size_) < 0) {
            ::close(shm_fd_); shm_fd_ = -1; return false;
        }

        shm_ptr_ = mmap(nullptr, shm_size_, PROT_READ | PROT_WRITE,
                         MAP_SHARED, shm_fd_, 0);
        if (shm_ptr_ == MAP_FAILED) {
            shm_ptr_ = nullptr; ::close(shm_fd_); shm_fd_ = -1; return false;
        }

        auto* h = reinterpret_cast<RingHeader*>(shm_ptr_);
        h->frame_counter.store(0, std::memory_order_relaxed);
        h->width = width;
        h->height = height;
        h->channels = channels;
        h->ring_size = RING_SIZE;
        h->write_index.store(0, std::memory_order_relaxed);
        h->latest_index.store(0, std::memory_order_relaxed);
        h->frame_size = frame_size;
        h->writer_pid = getpid();
        memset(h->timestamps, 0, sizeof(h->timestamps));
        memset(h->padding, 0, sizeof(h->padding));

        return true;
#else
        return false;
#endif
    }

    /**
     * Write a frame into the next ring slot and advance.
     * Called from the SCK delegate on the GCD capture_queue.
     */
    void write_frame(const uint8_t* pixels, uint32_t size, uint64_t timestamp_ns) {
        if (!shm_ptr_) return;

        auto* h = reinterpret_cast<RingHeader*>(shm_ptr_);
        uint32_t fs = h->frame_size;
        if (size < fs) return;

        // Write into the current write_index slot
        uint32_t wi = h->write_index.load(std::memory_order_relaxed);
        uint8_t* dst = static_cast<uint8_t*>(shm_ptr_)
                       + sizeof(RingHeader) + (wi * fs);

        memcpy(dst, pixels, fs);
        h->timestamps[wi] = timestamp_ns;

        // Publish: this slot is now the latest complete frame
        h->latest_index.store(wi, std::memory_order_release);
        h->frame_counter.fetch_add(1, std::memory_order_relaxed);

        // Advance write pointer (wrap around ring)
        h->write_index.store((wi + 1) % RING_SIZE, std::memory_order_relaxed);
    }

    /**
     * Write a stride-corrected frame directly from a padded pixel buffer.
     * Strips row padding in a single pass — no intermediate buffer.
     * Used when bytesPerRow > width * 4 (retina display padding).
     */
    void write_frame_strided(const uint8_t* pixels, uint32_t width,
                              uint32_t height, uint32_t src_stride,
                              uint64_t timestamp_ns) {
        if (!shm_ptr_) return;

        auto* h = reinterpret_cast<RingHeader*>(shm_ptr_);
        uint32_t fs = h->frame_size;
        uint32_t tight_row = width * h->channels;

        uint32_t wi = h->write_index.load(std::memory_order_relaxed);
        uint8_t* dst = static_cast<uint8_t*>(shm_ptr_)
                       + sizeof(RingHeader) + (wi * fs);

        // Row-by-row copy stripping padding — one pass, direct to SHM
        for (uint32_t y = 0; y < height; y++) {
            memcpy(dst + y * tight_row, pixels + y * src_stride, tight_row);
        }

        h->timestamps[wi] = timestamp_ns;
        h->latest_index.store(wi, std::memory_order_release);
        h->frame_counter.fetch_add(1, std::memory_order_relaxed);
        h->write_index.store((wi + 1) % RING_SIZE, std::memory_order_relaxed);
    }

    void close() {
#ifdef __APPLE__
        if (shm_ptr_) { munmap(shm_ptr_, shm_size_); shm_ptr_ = nullptr; }
        if (shm_fd_ >= 0) { ::close(shm_fd_); shm_unlink(SHM_NAME); shm_fd_ = -1; }
#endif
    }

    bool is_open() const { return shm_ptr_ != nullptr; }

private:
    int shm_fd_;
    void* shm_ptr_;
    size_t shm_size_;
};

}}} // namespace jarvis::vision::shm
