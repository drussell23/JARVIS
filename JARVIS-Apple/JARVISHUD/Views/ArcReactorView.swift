//
//  ArcReactorView.swift
//  JARVIS-HUD
//
//  Arc Reactor implementation matching web app CSS exactly
//  Based on JarvisVoice.css lines 132-385
//

import SwiftUI

/// Arc Reactor matching web app CSS implementation exactly
/// Core: 135px with radial gradient (cyan to bright center)
/// Ring 1: 210px (tech blue)
/// Ring 2: 285px (electric cyan)
/// Ring 3: 360px (neon purple)
/// Outer field: 440px (subtle cyan glow)
struct ArcReactorView: View {

    let state: HUDState
    var onQuit: (() -> Void)? = nil  // Callback when reactor is clicked

    @State private var coreScale: CGFloat = 1.0
    @State private var innerCoreScale: CGFloat = 1.0
    @State private var ring1Rotation: Double = 0
    @State private var ring2Rotation: Double = 0
    @State private var ring3Rotation: Double = 0
    @State private var ringOpacity: Double = 0.4
    @State private var isHovering: Bool = false

    var body: some View {
        ZStack {
            // Outer field (440px) - CSS line 357-373
            Circle()
                .stroke(Color(red: 0, green: 217/255, blue: 255/255, opacity: 0.2), lineWidth: 1)
                .frame(width: 440, height: 440)
                .shadow(color: Color(red: 0, green: 217/255, blue: 255/255, opacity: 0.2), radius: 40)
                .shadow(color: Color(red: 0, green: 217/255, blue: 255/255, opacity: 0.1), radius: 40)
                .opacity(ringOpacity)

            // Ring 3 - 360px (neon purple) - CSS line 305-312
            Circle()
                .stroke(Color(red: 138/255, green: 43/255, blue: 226/255), lineWidth: 3)
                .frame(width: 360, height: 360)
                .shadow(color: Color(red: 138/255, green: 43/255, blue: 226/255), radius: 25)
                .opacity(ringOpacity)
                .rotationEffect(.degrees(ring3Rotation))

            // Ring 2 - 285px (electric cyan) - CSS line 296-303
            Circle()
                .stroke(Color(red: 0, green: 255/255, blue: 255/255), lineWidth: 3)
                .frame(width: 285, height: 285)
                .shadow(color: Color(red: 0, green: 255/255, blue: 255/255), radius: 25)
                .opacity(ringOpacity)
                .rotationEffect(.degrees(ring2Rotation))

            // Ring 1 - 210px (tech blue) - CSS line 287-294
            Circle()
                .stroke(Color(red: 14/255, green: 165/255, blue: 233/255), lineWidth: 3)
                .frame(width: 210, height: 210)
                .shadow(color: Color(red: 14/255, green: 165/255, blue: 233/255), radius: 25)
                .opacity(ringOpacity)
                .rotationEffect(.degrees(ring1Rotation))

            // Core (135px) - CSS line 148-167
            Circle()
                .fill(
                    RadialGradient(
                        colors: [
                            Color(red: 0, green: 255/255, blue: 255/255), // Cyan
                            Color(red: 0, green: 217/255, blue: 255/255), // Bright cyan
                            Color(red: 14/255, green: 165/255, blue: 233/255) // Blue
                        ],
                        center: .center,
                        startRadius: 0,
                        endRadius: 67.5
                    )
                )
                .frame(width: 135, height: 135)
                .overlay(
                    Circle()
                        .stroke(Color(red: 0, green: 217/255, blue: 255/255, opacity: 0.5), lineWidth: 3)
                )
                .shadow(color: Color(red: 14/255, green: 165/255, blue: 233/255), radius: 50)
                .shadow(color: Color(red: 0, green: 255/255, blue: 255/255), radius: 100)
                .shadow(color: Color(red: 0, green: 217/255, blue: 255/255, opacity: 0.3), radius: 150)
                .scaleEffect(coreScale)

            // Inner core (70px) - CSS line 242-256
            Circle()
                .fill(
                    RadialGradient(
                        colors: [
                            Color.white.opacity(0.9),
                            Color(red: 0, green: 217/255, blue: 255/255, opacity: 0.5),
                            Color.clear
                        ],
                        center: .center,
                        startRadius: 0,
                        endRadius: 35
                    )
                )
                .frame(width: 70, height: 70)
                .scaleEffect(innerCoreScale)
        }
        .onAppear {
            startAnimations()
        }
        .onChange(of: state) { newState in
            updateReactorForState(newState)
        }
        .onTapGesture {
            // Make reactor clickable to quit HUD
            handleReactorClick()
        }
        .scaleEffect(isHovering ? 1.05 : 1.0)
        .animation(.easeInOut(duration: 0.2), value: isHovering)
        .onHover { hovering in
            isHovering = hovering
        }
        .help("Click to quit JARVIS HUD")  // Tooltip on hover
    }

    private func startAnimations() {
        // Core idle pulse animation - CSS line 169-189
        withAnimation(
            Animation.easeInOut(duration: 4.0)
                .repeatForever(autoreverses: true)
        ) {
            coreScale = 1.1
        }

        // Inner core shimmer - CSS line 258-270
        withAnimation(
            Animation.easeInOut(duration: 1.5)
                .repeatForever(autoreverses: true)
        ) {
            innerCoreScale = 1.3
        }

        // Ring rotations - CSS line 314-342
        withAnimation(
            Animation.linear(duration: 20.0)
                .repeatForever(autoreverses: false)
        ) {
            ring1Rotation = 360
        }

        withAnimation(
            Animation.linear(duration: 15.0)
                .repeatForever(autoreverses: false)
        ) {
            ring2Rotation = -360 // Reverse direction
        }

        withAnimation(
            Animation.linear(duration: 25.0)
                .repeatForever(autoreverses: false)
        ) {
            ring3Rotation = 360
        }

        // Ring pulse - CSS line 344-354
        withAnimation(
            Animation.easeInOut(duration: 3.0)
                .repeatForever(autoreverses: true)
        ) {
            ringOpacity = 0.8
        }
    }

    private func updateReactorForState(_ newState: HUDState) {
        switch newState {
        case .listening:
            // Gold gradient - CSS line 192-199
            break
        case .processing:
            // Purple gradient with spin - CSS line 214-231
            break
        case .idle, .speaking, .offline:
            // Keep cyan gradient
            break
        }
    }

    /// Handle reactor click to quit HUD
    private func handleReactorClick() {
        print("🎯 Arc Reactor clicked - triggering quit callback")
        onQuit?()
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        ArcReactorView(state: .idle)
    }
}
