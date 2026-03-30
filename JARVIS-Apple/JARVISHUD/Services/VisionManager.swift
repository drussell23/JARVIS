//
//  VisionManager.swift
//  JARVIS-HUD
//
//  Advanced Vision Management System - Screen Analysis & Vision Commands
//  Captures screenshots, sends to backend for AI analysis, handles responses
//

import Foundation
import AppKit
import CoreGraphics
import Combine

/// Vision command types
enum VisionCommandType: String, Codable {
    case analyze = "analyze"                    // General screen analysis
    case click = "click"                        // Click on element by description
    case find = "find"                          // Find element on screen
    case read = "read"                          // Read text from screen
    case navigate = "navigate"                  // Navigate to UI element
    case describe = "describe"                  // Describe what's on screen
    case custom = "custom"                      // Custom vision query
}

/// Vision analysis result
struct VisionAnalysisResult: Codable {
    let success: Bool
    let analysis: String?
    let elements: [ScreenElement]?
    let error: String?
    let metadata: [String: AnyCodable]?

    struct ScreenElement: Codable {
        let type: String
        let text: String?
        let location: CGRect?
        let confidence: Double?
    }
}

/// Screenshot capture result
struct ScreenshotCapture {
    let image: NSImage
    let data: Data
    let timestamp: Date
    let displayID: CGDirectDisplayID
    let bounds: CGRect
}

/// Vision command request
struct VisionCommandRequest: Codable {
    let command: String
    let commandType: VisionCommandType
    let imageData: String  // Base64 encoded
    let displayInfo: DisplayInfo
    let metadata: [String: AnyCodable]?

    struct DisplayInfo: Codable {
        let displayID: UInt32
        let bounds: CGRectData
        let scaleFactor: CGFloat

        struct CGRectData: Codable {
            let x: CGFloat
            let y: CGFloat
            let width: CGFloat
            let height: CGFloat
        }
    }
}

/// Dynamic AnyCodable type for JSON flexibility
struct AnyCodable: Codable {
    let value: Any

    init(_ value: Any) {
        self.value = value
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()

        if let int = try? container.decode(Int.self) {
            value = int
        } else if let double = try? container.decode(Double.self) {
            value = double
        } else if let string = try? container.decode(String.self) {
            value = string
        } else if let bool = try? container.decode(Bool.self) {
            value = bool
        } else if let array = try? container.decode([AnyCodable].self) {
            value = array.map { $0.value }
        } else if let dict = try? container.decode([String: AnyCodable].self) {
            value = dict.mapValues { $0.value }
        } else {
            value = NSNull()
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()

        switch value {
        case let int as Int:
            try container.encode(int)
        case let double as Double:
            try container.encode(double)
        case let string as String:
            try container.encode(string)
        case let bool as Bool:
            try container.encode(bool)
        case let array as [Any]:
            try container.encode(array.map { AnyCodable($0) })
        case let dict as [String: Any]:
            try container.encode(dict.mapValues { AnyCodable($0) })
        default:
            try container.encodeNil()
        }
    }
}

/// Advanced Vision Manager with async screenshot capture and AI analysis
@MainActor
class VisionManager: ObservableObject {

    // MARK: - Published State

    @Published var isProcessing: Bool = false
    @Published var lastAnalysisResult: VisionAnalysisResult?
    @Published var lastScreenshot: ScreenshotCapture?
    @Published var visionCommandHistory: [VisionCommandRequest] = []

    // MARK: - Configuration

    private let apiBaseURL: URL
    private var cancellables = Set<AnyCancellable>()

    // Screenshot cache (last N screenshots)
    private var screenshotCache: [ScreenshotCapture] = []
    private let maxCacheSize = 5

    // Vision capabilities (loaded from backend)
    private var visionCapabilities: [String] = []
    private var visionAvailable = false

    // MARK: - Initialization

    init(apiBaseURL: URL) {
        self.apiBaseURL = apiBaseURL
        print("👁️ VisionManager.init() - Initializing advanced vision system")
        print("   API Base URL: \(apiBaseURL.absoluteString)")

        Task {
            await checkVisionAvailability()
            await loadVisionCapabilities()
        }
    }

    // MARK: - Backend Integration

    private func checkVisionAvailability() async {
        do {
            let visionHealthURL = apiBaseURL.appendingPathComponent("/vision/status")
            let (data, response) = try await URLSession.shared.data(from: visionHealthURL)

            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200,
               let status = try? JSONDecoder().decode([String: Bool].self, from: data),
               status["available"] == true {
                visionAvailable = true
                print("   ✓ Backend vision system available")
            }
        } catch {
            visionAvailable = false
            print("   ⚠️  Backend vision system unavailable: \(error.localizedDescription)")
        }
    }

    private func loadVisionCapabilities() async {
        do {
            let capabilitiesURL = apiBaseURL.appendingPathComponent("/vision/capabilities")
            let (data, _) = try await URLSession.shared.data(from: capabilitiesURL)

            if let capabilities = try? JSONDecoder().decode([String].self, from: data) {
                visionCapabilities = capabilities
                print("   ✓ Vision capabilities loaded: \(capabilities.joined(separator: ", "))")
            }
        } catch {
            print("   ⚠️  Could not load vision capabilities: \(error.localizedDescription)")
        }
    }

    // MARK: - Screenshot Capture

    /// Capture screenshot of primary display
    func captureScreenshot() async throws -> ScreenshotCapture {
        return try await captureScreenshot(displayID: CGMainDisplayID())
    }

    /// Capture screenshot of specific display
    func captureScreenshot(displayID: CGDirectDisplayID) async throws -> ScreenshotCapture {
        print("📸 Capturing screenshot for display \(displayID)")

        guard let cgImage = CGDisplayCreateImage(displayID) else {
            throw VisionError.screenshotCaptureFailed
        }

        let nsImage = NSImage(cgImage: cgImage, size: .zero)

        // Convert to PNG data
        guard let tiffData = nsImage.tiffRepresentation,
              let bitmapImage = NSBitmapImageRep(data: tiffData),
              let pngData = bitmapImage.representation(using: .png, properties: [:]) else {
            throw VisionError.imageConversionFailed
        }

        // Get display bounds
        let bounds = CGDisplayBounds(displayID)

        let capture = ScreenshotCapture(
            image: nsImage,
            data: pngData,
            timestamp: Date(),
            displayID: displayID,
            bounds: bounds
        )

        // Cache screenshot
        cacheScreenshot(capture)

        lastScreenshot = capture
        print("   ✓ Screenshot captured: \(Int(bounds.width))x\(Int(bounds.height)) @ \(capture.timestamp)")

        return capture
    }

    /// Capture all displays
    func captureAllDisplays() async throws -> [ScreenshotCapture] {
        var captures: [ScreenshotCapture] = []

        // Get all active displays
        var displayCount: UInt32 = 0
        var displays: [CGDirectDisplayID] = Array(repeating: 0, count: 16)

        guard CGGetActiveDisplayList(16, &displays, &displayCount) == .success else {
            throw VisionError.displayEnumerationFailed
        }

        for i in 0..<Int(displayCount) {
            let capture = try await captureScreenshot(displayID: displays[i])
            captures.append(capture)
        }

        print("   ✓ Captured \(captures.count) display(s)")
        return captures
    }

    private func cacheScreenshot(_ capture: ScreenshotCapture) {
        screenshotCache.append(capture)

        // Keep only last N screenshots
        if screenshotCache.count > maxCacheSize {
            screenshotCache.removeFirst(screenshotCache.count - maxCacheSize)
        }
    }

    // MARK: - Vision Commands

    /// Execute vision command with automatic screenshot
    func executeVisionCommand(_ command: String, type: VisionCommandType = .custom, metadata: [String: AnyCodable]? = nil) async throws -> VisionAnalysisResult {
        guard visionAvailable else {
            throw VisionError.visionUnavailable
        }

        isProcessing = true
        defer { isProcessing = false }

        // Capture screenshot
        let screenshot = try await captureScreenshot()

        // Prepare vision command request
        let displayInfo = VisionCommandRequest.DisplayInfo(
            displayID: UInt32(screenshot.displayID),
            bounds: VisionCommandRequest.DisplayInfo.CGRectData(
                x: screenshot.bounds.origin.x,
                y: screenshot.bounds.origin.y,
                width: screenshot.bounds.size.width,
                height: screenshot.bounds.size.height
            ),
            scaleFactor: NSScreen.main?.backingScaleFactor ?? 2.0
        )

        let request = VisionCommandRequest(
            command: command,
            commandType: type,
            imageData: screenshot.data.base64EncodedString(),
            displayInfo: displayInfo,
            metadata: metadata
        )

        // Send to backend
        return try await sendVisionCommand(request)
    }

    /// Send vision command to backend
    private func sendVisionCommand(_ request: VisionCommandRequest) async throws -> VisionAnalysisResult {
        print("👁️ Sending vision command: \(request.command.prefix(50))...")

        var urlRequest = URLRequest(url: apiBaseURL.appendingPathComponent("/vision/analyze"))
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        urlRequest.timeoutInterval = 60.0 // Vision analysis can take time

        urlRequest.httpBody = try JSONEncoder().encode(request)

        let (data, response) = try await URLSession.shared.data(for: urlRequest)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw VisionError.invalidResponse
        }

        guard httpResponse.statusCode == 200 else {
            throw VisionError.requestFailed(statusCode: httpResponse.statusCode)
        }

        let result = try JSONDecoder().decode(VisionAnalysisResult.self, from: data)

        lastAnalysisResult = result
        visionCommandHistory.append(request)

        // Keep only last 20 commands in history
        if visionCommandHistory.count > 20 {
            visionCommandHistory.removeFirst(visionCommandHistory.count - 20)
        }

        print("   ✓ Vision analysis complete: \(result.success ? "success" : "failed")")
        if let analysis = result.analysis {
            print("   Analysis: \(analysis.prefix(100))...")
        }

        return result
    }

    // MARK: - Convenience Methods

    /// Describe what's currently on screen
    func describeScreen() async throws -> String {
        let result = try await executeVisionCommand("Describe what you see on the screen", type: .describe)

        guard result.success, let analysis = result.analysis else {
            throw VisionError.analysisFailedWithMessage(result.error ?? "Unknown error")
        }

        return analysis
    }

    /// Find specific element on screen
    func findElement(_ description: String) async throws -> VisionAnalysisResult {
        return try await executeVisionCommand("Find: \(description)", type: .find)
    }

    /// Click on element described
    func clickElement(_ description: String) async throws -> VisionAnalysisResult {
        return try await executeVisionCommand("Click: \(description)", type: .click)
    }

    /// Read text from screen region
    func readScreen(region: CGRect? = nil) async throws -> String {
        let metadata: [String: AnyCodable]?
        if let region = region {
            metadata = [
                "region": AnyCodable([
                    "x": region.origin.x,
                    "y": region.origin.y,
                    "width": region.size.width,
                    "height": region.size.height
                ])
            ]
        } else {
            metadata = nil
        }

        let result = try await executeVisionCommand("Read all text on screen", type: .read, metadata: metadata)

        guard result.success, let analysis = result.analysis else {
            throw VisionError.analysisFailedWithMessage(result.error ?? "Unknown error")
        }

        return analysis
    }
}

// MARK: - Errors

enum VisionError: Error, LocalizedError {
    case screenshotCaptureFailed
    case imageConversionFailed
    case displayEnumerationFailed
    case visionUnavailable
    case invalidResponse
    case requestFailed(statusCode: Int)
    case analysisFailedWithMessage(String)

    var errorDescription: String? {
        switch self {
        case .screenshotCaptureFailed:
            return "Failed to capture screenshot"
        case .imageConversionFailed:
            return "Failed to convert image to PNG"
        case .displayEnumerationFailed:
            return "Failed to enumerate displays"
        case .visionUnavailable:
            return "Vision system unavailable"
        case .invalidResponse:
            return "Invalid response from vision service"
        case .requestFailed(let code):
            return "Vision request failed with status code \(code)"
        case .analysisFailedWithMessage(let message):
            return "Vision analysis failed: \(message)"
        }
    }
}
