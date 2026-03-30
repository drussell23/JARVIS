// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "JARVISKit",
    platforms: [
        .iOS(.v17),
        .watchOS(.v10),
        .macOS(.v14),
    ],
    products: [
        .library(name: "JARVISKit", targets: ["JARVISKit"]),
    ],
    targets: [
        .target(name: "JARVISKit"),
        .testTarget(
            name: "JARVISKitTests",
            dependencies: ["JARVISKit"],
            swiftSettings: [
                .unsafeFlags([
                    "-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                ]),
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    "-Xlinker", "-rpath",
                    "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                ]),
            ]
        ),
    ]
)
