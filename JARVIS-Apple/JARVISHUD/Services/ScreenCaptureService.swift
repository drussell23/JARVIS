/// ScreenCaptureService — Async VLA screenshot capture via ScreenCaptureKit.
/// Captures main display → resizes to 1280px wide → JPEG base64.
/// Requires Screen Recording permission (TCC). Returns nil gracefully if not granted.
import Foundation
import ScreenCaptureKit
import CoreGraphics
import ImageIO

struct ScreenCaptureService: Sendable {
    static let shared = ScreenCaptureService()

    // MARK: - Permission

    /// True if Screen Recording permission has been granted.
    var hasPermission: Bool {
        CGPreflightScreenCaptureAccess()
    }

    /// Trigger the system permission dialog once at startup so the user can grant access
    /// before the first voice command arrives.
    func requestPermission() {
        CGRequestScreenCaptureAccess()
    }

    // MARK: - Capture

    /// Captures the main display and returns a base64 JPEG string, or nil on failure.
    /// All heavy work runs on a background Task — never blocks @MainActor.
    func captureBase64() async -> String? {
        guard hasPermission else {
            print("[JARVIS Vision] Screen Recording permission not granted — skipping screenshot")
            return nil
        }
        guard #available(macOS 14.0, *) else {
            print("[JARVIS Vision] SCScreenshotManager requires macOS 14+")
            return nil
        }
        return await _capture()
    }

    // MARK: - Private

    @available(macOS 14.0, *)
    private func _capture() async -> String? {
        do {
            // Discover all shareable content (displays, windows)
            let content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: true
            )
            guard let display = content.displays.first else {
                print("[JARVIS Vision] No display found via SCShareableContent")
                return nil
            }

            // Target 1280px wide — keeps base64 payload under ~300KB
            let targetWidth = 1280
            let aspectRatio = CGFloat(display.height) / CGFloat(display.width)
            let targetHeight = max(1, Int(CGFloat(targetWidth) * aspectRatio))

            let config = SCStreamConfiguration()
            config.width = targetWidth
            config.height = targetHeight
            config.capturesAudio = false
            config.showsCursor = false

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let cgImage = try await SCScreenshotManager.captureImage(
                contentFilter: filter,
                configuration: config
            )

            return _encodeJPEGBase64(cgImage)
        } catch {
            print("[JARVIS Vision] Capture failed: \(error.localizedDescription)")
            return nil
        }
    }

    private func _encodeJPEGBase64(_ cgImage: CGImage) -> String? {
        let buffer = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(
            buffer,
            "public.jpeg" as CFString,
            1,
            nil
        ) else { return nil }

        CGImageDestinationAddImage(
            dest,
            cgImage,
            [kCGImageDestinationLossyCompressionQuality: 0.6] as CFDictionary
        )
        guard CGImageDestinationFinalize(dest) else { return nil }

        let base64 = (buffer as Data).base64EncodedString()
        print("[JARVIS Vision] Captured \(cgImage.width)×\(cgImage.height) — \(buffer.length / 1024)KB → \(base64.count / 1024)KB base64")
        return base64
    }
}
