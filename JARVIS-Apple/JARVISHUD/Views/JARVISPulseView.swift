//
//  JARVISPulseView.swift
//  JARVIS-HUD
//
//  Core JARVIS pulse animation with concentric green rings
//  Matches web app's arc reactor and pulse effects
//

import SwiftUI

/// Animated pulse rings matching JARVIS web app aesthetic
struct JARVISPulseView: View {

    @State private var isPulsing = false
    @State private var scale1: CGFloat = 1.0
    @State private var scale2: CGFloat = 1.0
    @State private var scale3: CGFloat = 1.0
    @State private var opacity1: Double = 0.8
    @State private var opacity2: Double = 0.6
    @State private var opacity3: Double = 0.4

    var isActive: Bool = true

    var body: some View {
        ZStack {
            // Ring 3 (outermost)
            Circle()
                .stroke(
                    Color.jarvisGreen,
                    lineWidth: 3
                )
                .frame(width: 200, height: 200)
                .scaleEffect(scale3)
                .opacity(opacity3)
                .shadow(color: Color.jarvisGreenGlow(opacity: 0.8), radius: 20)

            // Ring 2 (middle)
            Circle()
                .stroke(
                    Color.jarvisGreen,
                    lineWidth: 4
                )
                .frame(width: 150, height: 150)
                .scaleEffect(scale2)
                .opacity(opacity2)
                .shadow(color: Color.jarvisGreenGlow(opacity: 0.9), radius: 15)

            // Ring 1 (innermost)
            Circle()
                .stroke(
                    Color.jarvisGreen,
                    lineWidth: 5
                )
                .frame(width: 100, height: 100)
                .scaleEffect(scale1)
                .opacity(opacity1)
                .shadow(color: Color.jarvisGreenGlow(opacity: 1.0), radius: 10)

            // Center core
            Circle()
                .fill(Color.jarvisGreen)
                .frame(width: 20, height: 20)
                .shadow(color: Color.jarvisGreenGlow(), radius: 30)
        }
        .onAppear {
            if isActive {
                startPulseAnimation()
            }
        }
        .onChange(of: isActive) { newValue in
            if newValue {
                startPulseAnimation()
            } else {
                stopPulseAnimation()
            }
        }
    }

    private func startPulseAnimation() {
        withAnimation(
            Animation.easeInOut(duration: 2.0)
                .repeatForever(autoreverses: true)
        ) {
            scale1 = 1.2
            opacity1 = 0.4
        }

        withAnimation(
            Animation.easeInOut(duration: 2.0)
                .repeatForever(autoreverses: true)
                .delay(0.3)
        ) {
            scale2 = 1.3
            opacity2 = 0.3
        }

        withAnimation(
            Animation.easeInOut(duration: 2.0)
                .repeatForever(autoreverses: true)
                .delay(0.6)
        ) {
            scale3 = 1.4
            opacity3 = 0.2
        }
    }

    private func stopPulseAnimation() {
        withAnimation(.easeOut(duration: 0.5)) {
            scale1 = 1.0
            scale2 = 1.0
            scale3 = 1.0
            opacity1 = 0.8
            opacity2 = 0.6
            opacity3 = 0.4
        }
    }
}

/// Alternative cyan pulse for different states (e.g., listening mode)
struct JARVISPulseCyanView: View {

    @State private var scale: CGFloat = 1.0
    @State private var opacity: Double = 0.8

    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.jarvisCyan, lineWidth: 4)
                .frame(width: 150, height: 150)
                .scaleEffect(scale)
                .opacity(opacity)
                .shadow(color: Color.jarvisCyanGlow(), radius: 20)

            Circle()
                .fill(Color.jarvisCyan)
                .frame(width: 15, height: 15)
                .shadow(color: Color.jarvisCyanGlow(), radius: 25)
        }
        .onAppear {
            withAnimation(
                Animation.easeInOut(duration: 1.5)
                    .repeatForever(autoreverses: true)
            ) {
                scale = 1.3
                opacity = 0.3
            }
        }
    }
}

#Preview {
    ZStack {
        Color.jarvisBlack.ignoresSafeArea()
        VStack(spacing: 50) {
            JARVISPulseView(isActive: true)
            JARVISPulseCyanView()
        }
    }
}
