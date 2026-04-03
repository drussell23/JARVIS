// JARVISHUD/Views/LivingBorderWindow.swift
// The Living Border — JARVIS's heartbeat.
// A full-screen borderless window that renders an animated glow around
// the screen edges. Ignores ALL mouse events. Takes zero workspace.

import AppKit
import QuartzCore

/// Cognitive state drives border color and animation speed
enum BorderState: String {
    case baseline   // Green, 4s breath
    case rem        // Purple, 3s pulse
    case flow       // Orange, 2s pulse
    case alert      // Red, 1.5s pulse
    case offline    // Dim gray, no animation

    var color: NSColor {
        switch self {
        case .baseline: return NSColor(red: 0.29, green: 0.87, blue: 0.50, alpha: 1.0) // #4ade80
        case .rem:      return NSColor(red: 0.65, green: 0.55, blue: 0.98, alpha: 1.0) // #a78bfa
        case .flow:     return NSColor(red: 0.98, green: 0.45, blue: 0.09, alpha: 1.0) // #f97316
        case .alert:    return NSColor(red: 0.94, green: 0.27, blue: 0.27, alpha: 1.0) // #ef4444
        case .offline:  return NSColor(red: 0.39, green: 0.45, blue: 0.55, alpha: 1.0) // #64748b
        }
    }

    var breathDuration: CFTimeInterval {
        switch self {
        case .baseline: return 4.0
        case .rem:      return 3.0
        case .flow:     return 2.0
        case .alert:    return 1.5
        case .offline:  return 0  // No animation
        }
    }

    var glowIntensity: CGFloat {
        switch self {
        case .baseline: return 0.15
        case .rem:      return 0.20
        case .flow:     return 0.25
        case .alert:    return 0.30
        case .offline:  return 0.05
        }
    }
}

class LivingBorderWindow: NSWindow {

    private var borderLayer: CAShapeLayer?
    private var glowLayer: CAShapeLayer?
    private var currentState: BorderState = .baseline

    init() {
        guard let screen = NSScreen.main else {
            super.init(contentRect: .zero, styleMask: .borderless, backing: .buffered, defer: false)
            return
        }

        super.init(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)

        // Window properties — completely invisible to interaction
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = false
        self.level = .screenSaver  // Above everything but doesn't capture input
        self.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        self.ignoresMouseEvents = true  // CRITICAL: passes ALL events through
        self.titlebarAppearsTransparent = true
        self.titleVisibility = .hidden

        // Set up the content view with layers
        let view = NSView(frame: screen.frame)
        view.wantsLayer = true
        view.layer?.backgroundColor = .clear
        self.contentView = view

        setupBorderLayers()
        setFrame(screen.frame, display: true)
        orderFrontRegardless()
    }

    // MARK: - Border Rendering

    private func setupBorderLayers() {
        guard let view = contentView, let layer = view.layer else { return }
        let bounds = view.bounds

        // Outer glow layer (soft, wide)
        let glow = CAShapeLayer()
        glow.path = CGPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), cornerWidth: 0, cornerHeight: 0, transform: nil)
        glow.fillColor = nil
        glow.strokeColor = currentState.color.withAlphaComponent(currentState.glowIntensity * 0.5).cgColor
        glow.lineWidth = 8
        glow.shadowColor = currentState.color.cgColor
        glow.shadowRadius = 15
        glow.shadowOpacity = Float(currentState.glowIntensity)
        glow.shadowOffset = .zero
        layer.addSublayer(glow)
        glowLayer = glow

        // Inner border layer (sharp, thin)
        let border = CAShapeLayer()
        border.path = CGPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), cornerWidth: 0, cornerHeight: 0, transform: nil)
        border.fillColor = nil
        border.strokeColor = currentState.color.withAlphaComponent(currentState.glowIntensity).cgColor
        border.lineWidth = 2
        layer.addSublayer(border)
        borderLayer = border

        // Start breathing animation
        startBreathing()
    }

    private func startBreathing() {
        guard currentState != .offline else {
            borderLayer?.removeAllAnimations()
            glowLayer?.removeAllAnimations()
            return
        }

        let duration = currentState.breathDuration
        let intensity = currentState.glowIntensity
        let color = currentState.color

        // Glow opacity animation
        let glowAnim = CABasicAnimation(keyPath: "shadowOpacity")
        glowAnim.fromValue = Float(intensity * 0.3)
        glowAnim.toValue = Float(intensity)
        glowAnim.duration = duration
        glowAnim.autoreverses = true
        glowAnim.repeatCount = .infinity
        glowAnim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        glowLayer?.add(glowAnim, forKey: "breathe-glow")

        // Border opacity animation
        let borderAnim = CABasicAnimation(keyPath: "strokeColor")
        borderAnim.fromValue = color.withAlphaComponent(intensity * 0.4).cgColor
        borderAnim.toValue = color.withAlphaComponent(intensity).cgColor
        borderAnim.duration = duration
        borderAnim.autoreverses = true
        borderAnim.repeatCount = .infinity
        borderAnim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        borderLayer?.add(borderAnim, forKey: "breathe-border")

        // Shadow radius pulse
        let radiusAnim = CABasicAnimation(keyPath: "shadowRadius")
        radiusAnim.fromValue = 8
        radiusAnim.toValue = 20
        radiusAnim.duration = duration
        radiusAnim.autoreverses = true
        radiusAnim.repeatCount = .infinity
        radiusAnim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        glowLayer?.add(radiusAnim, forKey: "breathe-radius")
    }

    // MARK: - State Updates

    func updateState(_ newState: BorderState) {
        guard newState != currentState else { return }
        currentState = newState

        let color = newState.color
        let intensity = newState.glowIntensity

        // Update colors
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.5)
        borderLayer?.strokeColor = color.withAlphaComponent(intensity).cgColor
        glowLayer?.strokeColor = color.withAlphaComponent(intensity * 0.5).cgColor
        glowLayer?.shadowColor = color.cgColor
        glowLayer?.shadowOpacity = Float(intensity)
        CATransaction.commit()

        // Restart breathing with new speed
        startBreathing()
    }
}
