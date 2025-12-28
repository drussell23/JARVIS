/**
 * Python bindings for Fast Capture Streaming Engine
 * Uses pybind11 for seamless Python integration
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>
#include <pybind11/chrono.h>
#include <pybind11/functional.h>
#include <sstream>
#include <iomanip>

#include "fast_capture_stream.h"

namespace py = pybind11;
using namespace jarvis::vision;

// Convert StreamFrame to Python dict with numpy array
py::dict stream_frame_to_dict(const StreamFrame& frame) {
    py::dict d;

    d["width"] = frame.width;
    d["height"] = frame.height;
    d["channels"] = frame.channels;
    d["format"] = frame.format;
    d["frame_number"] = frame.frame_number;
    d["timestamp"] = frame.timestamp;
    d["capture_latency_us"] = frame.capture_latency.count();
    d["gpu_accelerated"] = frame.gpu_accelerated;
    d["memory_used"] = frame.memory_used;

    // Convert image data to numpy array if raw format
    if (frame.format == "raw" && !frame.data.empty()) {
        auto result = py::array_t<uint8_t>({frame.height, frame.width, frame.channels});
        auto buf = result.request();
        uint8_t* ptr = static_cast<uint8_t*>(buf.ptr);
        std::memcpy(ptr, frame.data.data(), frame.data.size());
        d["image"] = result;
    } else {
        // For compressed formats, return bytes
        d["image_data"] = py::bytes(reinterpret_cast<const char*>(frame.data.data()),
                                   frame.data.size());
    }

    return d;
}

// Module definition
PYBIND11_MODULE(fast_capture_stream, m) {
    m.doc() = "Fast Screen Capture Streaming Engine - Persistent 60 FPS ScreenCaptureKit Streams";

    // ===== Constants =====
    m.attr("VERSION") = "1.0.0";

    // ===== StreamFrame =====
    py::class_<StreamFrame>(m, "StreamFrame")
        .def(py::init<>())
        .def_readonly("width", &StreamFrame::width)
        .def_readonly("height", &StreamFrame::height)
        .def_readonly("channels", &StreamFrame::channels)
        .def_readonly("format", &StreamFrame::format)
        .def_readonly("frame_number", &StreamFrame::frame_number)
        .def_readonly("timestamp", &StreamFrame::timestamp)
        .def_readonly("capture_latency", &StreamFrame::capture_latency)
        .def_readonly("gpu_accelerated", &StreamFrame::gpu_accelerated)
        .def_readonly("memory_used", &StreamFrame::memory_used)
        .def("to_dict", &stream_frame_to_dict, "Convert to dictionary with numpy array");

    // ===== StreamConfig =====
    py::class_<StreamConfig>(m, "StreamConfig")
        .def(py::init<>())
        .def_readwrite("target_fps", &StreamConfig::target_fps,
                      "Target FPS (1-60)")
        .def_readwrite("max_buffer_size", &StreamConfig::max_buffer_size,
                      "Maximum frame buffer size (0 = unbounded)")
        .def_readwrite("output_format", &StreamConfig::output_format,
                      "Output format: 'raw', 'jpeg', 'png'")
        .def_readwrite("jpeg_quality", &StreamConfig::jpeg_quality,
                      "JPEG quality (1-100)")
        .def_readwrite("use_gpu_acceleration", &StreamConfig::use_gpu_acceleration,
                      "Enable GPU acceleration")
        .def_readwrite("drop_frames_on_overflow", &StreamConfig::drop_frames_on_overflow,
                      "Drop oldest frames if buffer full")
        .def_readwrite("capture_cursor", &StreamConfig::capture_cursor,
                      "Capture cursor in frames")
        .def_readwrite("capture_shadow", &StreamConfig::capture_shadow,
                      "Capture window shadows")
        .def_readwrite("resolution_scale", &StreamConfig::resolution_scale,
                      "Resolution scale (1.0 = native, 0.5 = half, 2.0 = retina)")
        .def("set_frame_callback", [](StreamConfig& self, py::function callback) {
            self.frame_callback = [callback](const StreamFrame& frame) {
                py::gil_scoped_acquire acquire;
                callback(stream_frame_to_dict(frame));
            };
        }, py::arg("callback"),
           "Set callback for each frame (called on capture thread)")
        .def("set_error_callback", [](StreamConfig& self, py::function callback) {
            self.error_callback = [callback](const std::string& error) {
                py::gil_scoped_acquire acquire;
                callback(error);
            };
        }, py::arg("callback"),
           "Set callback for errors");

    // ===== StreamStats =====
    py::class_<StreamStats>(m, "StreamStats")
        .def(py::init<>())
        .def_readonly("total_frames", &StreamStats::total_frames)
        .def_readonly("dropped_frames", &StreamStats::dropped_frames)
        .def_readonly("actual_fps", &StreamStats::actual_fps)
        .def_readonly("avg_latency_ms", &StreamStats::avg_latency_ms)
        .def_readonly("min_latency_ms", &StreamStats::min_latency_ms)
        .def_readonly("max_latency_ms", &StreamStats::max_latency_ms)
        .def_readonly("current_buffer_size", &StreamStats::current_buffer_size)
        .def_readonly("peak_buffer_size", &StreamStats::peak_buffer_size)
        .def_readonly("bytes_processed", &StreamStats::bytes_processed)
        .def_readonly("stream_start_time", &StreamStats::stream_start_time)
        .def_readonly("is_active", &StreamStats::is_active)
        .def("__repr__", [](const StreamStats& s) {
            std::ostringstream oss;
            oss << "<StreamStats: "
                << s.total_frames << " frames, "
                << std::fixed << std::setprecision(1) << s.actual_fps << " FPS, "
                << std::setprecision(2) << s.avg_latency_ms << "ms latency, "
                << (s.is_active ? "ACTIVE" : "STOPPED") << ">";
            return oss.str();
        });

    // ===== CaptureStream =====
    py::class_<CaptureStream>(m, "CaptureStream")
        .def(py::init<uint32_t, const StreamConfig&>(),
             py::arg("window_id"),
             py::arg("config") = StreamConfig(),
             "Create a continuous capture stream for a window")

        // Stream control
        .def("start", &CaptureStream::start,
             "Start the capture stream")
        .def("stop", &CaptureStream::stop,
             "Stop the capture stream")
        .def("is_active", &CaptureStream::is_active,
             "Check if stream is active")

        // Frame access
        .def("get_frame", [](CaptureStream& self, int timeout_ms) -> py::object {
            auto frame = self.get_frame(std::chrono::milliseconds(timeout_ms));
            if (frame) {
                return stream_frame_to_dict(*frame);
            }
            return py::none();
        }, py::arg("timeout_ms") = 100,
           "Get latest frame (blocking with timeout)")

        .def("try_get_frame", [](CaptureStream& self) -> py::object {
            auto frame = self.try_get_frame();
            if (frame) {
                return stream_frame_to_dict(*frame);
            }
            return py::none();
        }, "Get latest frame (non-blocking)")

        .def("get_all_frames", [](CaptureStream& self) {
            auto frames = self.get_all_frames();
            py::list result;
            for (const auto& frame : frames) {
                result.append(stream_frame_to_dict(frame));
            }
            return result;
        }, "Get all available frames (drains buffer)")

        // Statistics
        .def("get_stats", &CaptureStream::get_stats,
             "Get stream statistics")
        .def("reset_stats", &CaptureStream::reset_stats,
             "Reset statistics")

        // Configuration
        .def("update_config", &CaptureStream::update_config,
             py::arg("config"),
             "Update stream configuration (restarts stream)")
        .def("get_config", &CaptureStream::get_config,
             "Get current configuration")

        // Window info
        .def("get_window_id", &CaptureStream::get_window_id,
             "Get window ID being captured")
        .def("get_window_info", &CaptureStream::get_window_info,
             "Get window information")

        .def("__repr__", [](const CaptureStream& s) {
            std::ostringstream oss;
            oss << "<CaptureStream window_id=" << s.get_window_id()
                << " active=" << (s.is_active() ? "true" : "false") << ">";
            return oss.str();
        });

    // ===== StreamManager =====
    py::class_<StreamManager>(m, "StreamManager")
        .def(py::init<>(),
             "Create a stream manager for multiple concurrent streams")

        // Stream management
        .def("create_stream", &StreamManager::create_stream,
             py::arg("window_id"),
             py::arg("config") = StreamConfig(),
             "Create and start a new stream, returns stream ID")

        .def("create_stream_by_name", &StreamManager::create_stream_by_name,
             py::arg("app_name"),
             py::arg("window_title") = "",
             py::arg("config") = StreamConfig(),
             "Create stream from window name")

        .def("destroy_stream", &StreamManager::destroy_stream,
             py::arg("stream_id"),
             "Stop and destroy a stream")

        .def("destroy_all_streams", &StreamManager::destroy_all_streams,
             "Stop all streams")

        // Frame access
        .def("get_frame", [](StreamManager& self, const std::string& stream_id, int timeout_ms) -> py::object {
            auto frame = self.get_frame(stream_id, std::chrono::milliseconds(timeout_ms));
            if (frame) {
                return stream_frame_to_dict(*frame);
            }
            return py::none();
        }, py::arg("stream_id"), py::arg("timeout_ms") = 100,
           "Get frame from specific stream")

        .def("get_all_frames", [](StreamManager& self, int timeout_ms) {
            auto frames = self.get_all_frames(std::chrono::milliseconds(timeout_ms));
            py::dict result;
            for (const auto& [id, frame] : frames) {
                result[py::str(id)] = stream_frame_to_dict(frame);
            }
            return result;
        }, py::arg("timeout_ms") = 100,
           "Get frames from all active streams")

        // Stream info
        .def("get_active_stream_ids", &StreamManager::get_active_stream_ids,
             "Get list of active stream IDs")

        .def("get_stream_stats", &StreamManager::get_stream_stats,
             py::arg("stream_id"),
             "Get statistics for specific stream")

        .def("get_all_stats", &StreamManager::get_all_stats,
             "Get statistics for all streams")

        // Resource management
        .def("get_active_stream_count", &StreamManager::get_active_stream_count,
             "Get number of active streams")

        .def("get_total_memory_usage", &StreamManager::get_total_memory_usage,
             "Get total memory usage across all streams")

        .def("set_max_concurrent_streams", &StreamManager::set_max_concurrent_streams,
             py::arg("max"),
             "Set maximum number of concurrent streams")

        .def("__repr__", [](const StreamManager& m) {
            std::ostringstream oss;
            oss << "<StreamManager active_streams=" << m.get_active_stream_count() << ">";
            return oss.str();
        });

    // ===== Utility Functions =====
    m.def("is_screencapturekit_available", &is_screencapturekit_available,
          "Check if ScreenCaptureKit is available (requires macOS 12.3+)");

    m.def("get_recommended_fps", &get_recommended_fps,
          py::arg("width"),
          py::arg("height"),
          py::arg("gpu_available") = true,
          "Get recommended FPS based on window size and capabilities");

    m.def("estimate_stream_memory", &estimate_stream_memory,
          py::arg("config"),
          py::arg("width"),
          py::arg("height"),
          "Estimate memory usage for stream configuration");
}
