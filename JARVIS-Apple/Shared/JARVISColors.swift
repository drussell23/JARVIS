//
//  JARVISColors.swift
//  JARVIS-HUD
//
//  Color system matching the JARVIS web app theme
//  Extracted from frontend CSS files to maintain visual consistency
//

import SwiftUI

/// JARVIS Color Palette - Direct translation from web app CSS
/// Maintains exact colors from JarvisVoice.css, App.css, and index.css
extension Color {

    // MARK: - Primary Colors (from App.css and JarvisVoice.css)

    /// Primary neon green - #00ff41 (Matrix green, main accent)
    static let jarvisGreen = Color(hex: "00ff41")

    /// Secondary green - #00aa2e (darker green for gradients)
    static let jarvisGreenDark = Color(hex: "00aa2e")

    /// Alternative green from PRD - #00ff99
    static let jarvisGreenAlt = Color(hex: "00ff99")

    // MARK: - Cyan/Blue Accents (from JarvisVoice.css)

    /// Primary cyan - #00FFFF (arc reactor, borders)
    static let jarvisCyan = Color(hex: "00FFFF")

    /// Cyan variant - #00D9FF (glows)
    static let jarvisCyanBright = Color(hex: "00D9FF")

    /// Blue accent - #0EA5E9 (highlights)
    static let jarvisBlue = Color(hex: "0EA5E9")

    /// Status cyan - #00d4ff (voice stats)
    static let jarvisCyanStatus = Color(hex: "00d4ff")

    // MARK: - Background Colors (from index.css, App.css)

    /// Pure black - #000000 (primary background)
    static let jarvisBlack = Color(hex: "000000")

    /// Dark blue-black - #0a0f1c (gradient background)
    static let jarvisBlackBlue = Color(hex: "0a0f1c")

    /// Near-black - #050505 (landing page variant)
    static let jarvisBlackAlt = Color(hex: "050505")

    /// Dark overlay - rgba(10, 10, 10, 0.9)
    static let jarvisOverlayDark = Color(red: 10/255, green: 10/255, blue: 10/255, opacity: 0.9)

    // MARK: - Status Colors

    /// Success green - #00ff88
    static let jarvisSuccess = Color(hex: "00ff88")

    /// Success alt - #4ade80
    static let jarvisSuccessAlt = Color(hex: "4ade80")

    /// Success standard - #4caf50
    static let jarvisSuccessStandard = Color(hex: "4caf50")

    /// Warning orange - #ffaa00
    static let jarvisWarning = Color(hex: "ffaa00")

    /// Warning alt - #ff9800
    static let jarvisWarningAlt = Color(hex: "ff9800")

    /// Error red - #f44336
    static let jarvisError = Color(hex: "f44336")

    /// Error bright - #ff0000
    static let jarvisErrorBright = Color(hex: "ff0000")

    // MARK: - Accent Colors

    /// Gold - #ffd700 (workspace monitor, Iron Man theme)
    static let jarvisGold = Color(hex: "ffd700")

    /// Purple primary - #9d4edd (audio quality)
    static let jarvisPurple = Color(hex: "9d4edd")

    /// Purple light - #c77dff
    static let jarvisPurpleLight = Color(hex: "c77dff")

    /// Purple dark - #7209b7
    static let jarvisPurpleDark = Color(hex: "7209b7")

    // MARK: - Glow Colors (for shadows and effects)

    /// Green glow - RGB(0, 255, 65) with opacity
    static func jarvisGreenGlow(opacity: Double = 0.6) -> Color {
        return Color(red: 0, green: 255/255, blue: 65/255, opacity: opacity)
    }

    /// Cyan glow - RGB(0, 255, 255) with opacity
    static func jarvisCyanGlow(opacity: Double = 0.6) -> Color {
        return Color(red: 0, green: 255/255, blue: 255/255, opacity: opacity)
    }

    /// Blue glow - RGBA(0, 174, 239, 0.15) from index.css
    static let jarvisBlueGlow = Color(red: 0, green: 174/255, blue: 239/255, opacity: 0.15)

    /// Pink accent - RGBA(255, 0, 102, 0.08) from index.css
    static let jarvisPinkGlow = Color(red: 255/255, green: 0, blue: 102/255, opacity: 0.08)

    // MARK: - Gradient Definitions

    /// Primary HUD gradient (green to dark green)
    static let jarvisGradientGreen = LinearGradient(
        colors: [jarvisGreen, jarvisGreenDark],
        startPoint: .top,
        endPoint: .bottom
    )

    /// Arc reactor gradient (cyan to blue)
    static let jarvisGradientCyan = LinearGradient(
        colors: [jarvisCyan, jarvisBlue],
        startPoint: .top,
        endPoint: .bottom
    )

    /// Background gradient (black to dark blue-black)
    static let jarvisGradientBackground = LinearGradient(
        colors: [jarvisBlack, jarvisBlackBlue],
        startPoint: .top,
        endPoint: .bottom
    )
}

// MARK: - Hex Color Initializer

extension Color {
    /// Initialize Color from hex string
    /// - Parameter hex: Hex color string (with or without #)
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var int: UInt64 = 0
        Scanner(string: hex).scanHexInt64(&int)

        let r, g, b: UInt64
        switch hex.count {
        case 6: // RGB
            r = (int >> 16) & 0xFF
            g = (int >> 8) & 0xFF
            b = int & 0xFF
            self.init(
                .sRGB,
                red: Double(r) / 255,
                green: Double(g) / 255,
                blue: Double(b) / 255,
                opacity: 1
            )
        default:
            self.init(.sRGB, red: 0, green: 0, blue: 0, opacity: 1)
        }
    }
}

// MARK: - JARVIS Theme Configuration

/// Complete theme configuration for JARVIS HUD
struct JARVISTheme {
    /// Primary accent color (neon green)
    static let primary = Color.jarvisGreen

    /// Secondary accent color (cyan)
    static let secondary = Color.jarvisCyan

    /// Background color (pure black)
    static let background = Color.jarvisBlack

    /// Text color (neon green)
    static let text = Color.jarvisGreen

    /// Glow intensity for effects
    static let glowRadius: CGFloat = 20

    /// Animation duration for pulse effects
    static let pulseDuration: Double = 2.0

    /// Font configuration
    struct Fonts {
        /// Monospace font for terminal-style text (matches JetBrains Mono / SF Mono)
        static let monospace = "SFMono-Regular"

        /// Display font for titles
        static let display = "SFMono-Bold"
    }
}
