import SwiftUI
import JARVISKit

struct PairingView: View {
    @State private var pairingCode = ""
    @State private var status = ""
    @State private var isPaired = KeychainStore.load(key: "device_id") != nil

    var body: some View {
        Form {
            if isPaired {
                Section("Device") {
                    HStack {
                        Text("Status")
                        Spacer()
                        Text("Paired").foregroundColor(.green)
                    }
                    if let deviceId = KeychainStore.load(key: "device_id") {
                        HStack {
                            Text("Device ID")
                            Spacer()
                            Text(deviceId).font(.caption).foregroundColor(.secondary)
                        }
                    }
                }
                Section {
                    Button("Unpair Device", role: .destructive) { unpair() }
                }
            } else {
                Section("Pair with JARVIS Cloud") {
                    TextField("Pairing Code", text: $pairingCode)
                        .textInputAutocapitalization(.characters)
                        .fontDesign(.monospaced)
                    Button("Pair") { Task { await pair() } }
                        .disabled(pairingCode.count < 8)
                }
                if !status.isEmpty {
                    Section { Text(status).foregroundColor(.red) }
                }
            }
        }
    }

    private func pair() async {
        // Call POST /api/devices/pair
        status = "Pairing..."
        guard let url = URL(string: "\(getBaseURL())/api/devices/pair") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let deviceId = "iphone-15pm-\(UUID().uuidString.prefix(8))"
        let body: [String: Any] = [
            "pairing_code": pairingCode,
            "device_id": deviceId,
            "device_type": "iphone",
            "device_name": "Derek's iPhone 15 Pro Max",
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                status = "Pairing failed — invalid code"
                return
            }
            let result = try JSONDecoder().decode(PairResult.self, from: data)
            KeychainStore.save(key: "device_id", value: deviceId)
            KeychainStore.save(key: "device_secret", value: result.deviceSecret)
            KeychainStore.save(key: "vercel_url", value: getBaseURL())
            isPaired = true
            status = ""
        } catch {
            status = "Error: \(error.localizedDescription)"
        }
    }

    private func unpair() {
        KeychainStore.delete(key: "device_id")
        KeychainStore.delete(key: "device_secret")
        isPaired = false
    }

    private func getBaseURL() -> String {
        KeychainStore.load(key: "vercel_url") ?? "https://jarvis-cloud-five.vercel.app"
    }
}

struct PairResult: Codable {
    let deviceSecret: String
    let streamEndpoint: String
    let commandEndpoint: String

    enum CodingKeys: String, CodingKey {
        case deviceSecret = "device_secret"
        case streamEndpoint = "stream_endpoint"
        case commandEndpoint = "command_endpoint"
    }
}
