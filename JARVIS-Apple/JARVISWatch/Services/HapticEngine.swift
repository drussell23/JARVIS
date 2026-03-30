import WatchKit

enum HapticPattern {
    case start, success, failure, notification, urgentAlert
}

enum HapticEngine {
    static func play(_ pattern: HapticPattern) {
        let device = WKInterfaceDevice.current()
        switch pattern {
        case .start: device.play(.click)
        case .success: device.play(.success)
        case .failure: device.play(.failure)
        case .notification: device.play(.notification)
        case .urgentAlert:
            device.play(.notification)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { device.play(.notification) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { device.play(.notification) }
        }
    }
}
