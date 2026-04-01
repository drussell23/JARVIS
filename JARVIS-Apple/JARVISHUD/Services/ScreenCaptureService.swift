/// ScreenCaptureService — Always-on SCStream for continuous VLA.
/// Maintains a persistent ScreenCaptureKit stream at 1 fps. The macOS purple recording
/// indicator stays visible, confirming JARVIS's eyes are open. Voice commands grab the
/// latest cached frame instantly (no capture delay).
import Foundation
import ScreenCaptureKit
import CoreGraphics
import ImageIO
import os

@available(macOS 14.0, *)
final class ScreenCaptureService: NSObject, SCStreamOutput, @unchecked Sendable {
    static let shared = ScreenCaptureService()

    /// True when the stream is actively capturing frames.
    private(set) var isStreaming = false

    // MARK: - Permission

    var hasPermission: Bool {
        CGPreflightScreenCaptureAccess()
    }

    func requestPermission() {
        if hasPermission { return }
        Task.detached {
            do {
                _ = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                print("[JARVIS Vision] Screen Recording permission granted")
            } catch {
                print("[JARVIS Vision] Screen Recording not yet granted — enable in System Settings > Privacy & Security > Screen Recording")
            }
        }
    }

    // MARK: - Stream lifecycle

    private var stream: SCStream?
    /// Swift 6-safe lock for the cached frame (OSAllocatedUnfairLock.withLock is async-context safe)
    private let frameStore = OSAllocatedUnfairLock<CGImage?>(initialState: nil)

    /// Start the persistent screen stream. Call once at app boot.
    /// The macOS purple recording indicator appears automatically.
    func startStream() {
        guard !isStreaming else { return }
        Task {
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                guard let display = content.displays.first else {
                    print("[JARVIS Vision] No display found — cannot start stream")
                    return
                }

                let config = SCStreamConfiguration()
                config.width = 1280
                let aspect = CGFloat(display.height) / CGFloat(display.width)
                config.height = max(1, Int(1280.0 * aspect))
                config.capturesAudio = false
                config.showsCursor = false
                // 1 fps — enough for voice-command context, minimal CPU
                config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

                let filter = SCContentFilter(display: display, excludingWindows: [])
                let newStream = SCStream(filter: filter, configuration: config, delegate: nil)
                try newStream.addStreamOutput(self, type: .screen, sampleHandlerQueue: .global(qos: .utility))
                try await newStream.startCapture()

                self.stream = newStream
                self.isStreaming = true
                print("[JARVIS Vision] Stream started — 1fps, \(config.width)×\(config.height), purple indicator active")
            } catch {
                print("[JARVIS Vision] Stream start failed: \(error.localizedDescription)")
            }
        }
    }

    func stopStream() {
        guard isStreaming, let stream else { return }
        Task {
            try? await stream.stopCapture()
            self.stream = nil
            self.isStreaming = false
            frameStore.withLock { $0 = nil }
            print("[JARVIS Vision] Stream stopped")
        }
    }

    // MARK: - SCStreamOutput (frame callback — runs on utility queue)

    nonisolated func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        guard let imageBuffer = sampleBuffer.imageBuffer else { return }

        let ciImage = CIImage(cvImageBuffer: imageBuffer)
        let ctx = CIContext()
        let rect = CGRect(x: 0, y: 0,
                          width: CVPixelBufferGetWidth(imageBuffer),
                          height: CVPixelBufferGetHeight(imageBuffer))
        guard let cgImage = ctx.createCGImage(ciImage, from: rect) else { return }

        frameStore.withLock { $0 = cgImage }

        // Write live frame to disk for Python backend to read between VLA steps.
        // The backend's JarvisCU reads /tmp/jarvis_live_frame.jpg after each
        // step execution to get an updated view of the screen.
        if let data = encodeJPEGData(cgImage) {
            try? data.write(to: URL(fileURLWithPath: "/tmp/jarvis_live_frame.jpg"), options: .atomic)
        }
    }

    private nonisolated func encodeJPEGData(_ cgImage: CGImage) -> Data? {
        let buf = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(buf, "public.jpeg" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, cgImage, [kCGImageDestinationLossyCompressionQuality: 0.7] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return buf as Data
    }

    // MARK: - Public API

    /// Returns the latest streamed frame as base64 JPEG, or falls back to one-shot.
    func captureBase64() async -> String? {
        // Fast path: grab cached frame from stream (instant, no API call)
        let cached = frameStore.withLock { $0 }

        if let cached {
            if let result = encodeJPEGBase64(cached) {
                print("[JARVIS Vision] Frame from stream — \(cached.width)×\(cached.height), \(result.count) chars")
                return result
            }
        }

        // Slow path: stream not running yet — one-shot capture
        print("[JARVIS Vision] No cached frame — one-shot fallback")
        return await oneShotCapture()
    }

    /// Force a FRESH screenshot (bypasses the 1fps stream cache).
    /// Use this when you need the current display state RIGHT NOW,
    /// e.g., after switching to a target app for VLA planning.
    func captureFresh() async -> String? {
        print("[JARVIS Vision] Fresh capture requested (bypassing cache)")
        return await oneShotCapture()
    }

    // MARK: - One-shot fallback

    private func oneShotCapture() async -> String? {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else { return nil }

            let config = SCStreamConfiguration()
            config.width = 1280
            let aspect = CGFloat(display.height) / CGFloat(display.width)
            config.height = max(1, Int(1280.0 * aspect))
            config.capturesAudio = false
            config.showsCursor = false

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let cgImage = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
            if let result = encodeJPEGBase64(cgImage) {
                print("[JARVIS Vision] One-shot \(cgImage.width)×\(cgImage.height), \(result.count) chars")
                return result
            }
            return nil
        } catch {
            print("[JARVIS Vision] One-shot failed: \(error.localizedDescription)")
            return nil
        }
    }

    // MARK: - JPEG encoding

    private func encodeJPEGBase64(_ cgImage: CGImage) -> String? {
        let buf = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(buf, "public.jpeg" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, cgImage, [kCGImageDestinationLossyCompressionQuality: 0.6] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return (buf as Data).base64EncodedString()
    }
}
