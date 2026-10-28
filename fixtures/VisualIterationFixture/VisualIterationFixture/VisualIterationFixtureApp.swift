import SwiftUI

@main
struct VisualIterationFixtureApp: App {
    var body: some Scene {
        WindowGroup { FixtureRootView() }
    }
}

private enum FixtureScreen: String {
    case home, details, settings
}

struct FixtureRootView: View {
    @State private var screen: FixtureScreen = .home
    @State private var challengeVisible = false
    @State private var counter = 0
    private let challengeToken: String = {
        let arguments = ProcessInfo.processInfo.arguments
        guard let flagIndex = arguments.firstIndex(of: "--hermes-challenge"), arguments.indices.contains(flagIndex + 1) else {
            return "missing"
        }
        let candidate = arguments[flagIndex + 1]
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        guard !candidate.isEmpty, candidate.count <= 128, candidate.unicodeScalars.allSatisfy(allowed.contains) else {
            return "invalid"
        }
        return candidate
    }()
    private var challengeResponse: String {
        "challenge-response:\(challengeToken):HERMES-IOS-U1-OK"
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Text("HERMES-IOS-U1-v1")
                    .font(.caption.monospaced())
                    .accessibilityIdentifier("fixture.version-token")
                TimelineView(.periodic(from: .now, by: 0.5)) { context in
                    Text("Frame \(Int(context.date.timeIntervalSince1970 * 2))")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("fixture.frame-heartbeat")
                }

                switch screen {
                case .home:
                    Text("U7 causal proof complete").font(.title2.bold())
                        .accessibilityIdentifier("fixture.home.title")
                    Button("Inspect Details") { screen = .details }
                        .accessibilityIdentifier("fixture.home.open-details")
                    Button("Open Settings") { screen = .settings }
                        .accessibilityIdentifier("fixture.home.open-settings")
                case .details:
                    Text("Deterministic Details").font(.title2.bold())
                        .accessibilityIdentifier("fixture.details.title")
                    Text("Counter: \(counter)")
                        .accessibilityIdentifier("fixture.details.counter")
                    Button("Increment") { counter += 1 }
                        .accessibilityIdentifier("fixture.details.increment")
                    Button("Back Home") { screen = .home }
                        .accessibilityIdentifier("fixture.details.back")
                case .settings:
                    Text("Deterministic Settings").font(.title2.bold())
                        .accessibilityIdentifier("fixture.settings.title")
                    Button("Back Home") { screen = .home }
                        .accessibilityIdentifier("fixture.settings.back")
                }

                Divider()
                Button("Run Challenge") { challengeVisible = true }
                    .accessibilityIdentifier("fixture.challenge.trigger")
                if challengeVisible {
                    Text(challengeResponse)
                        .font(.caption.monospaced())
                        .accessibilityIdentifier("fixture.challenge.response")
                }
                Button("Trigger Crash", role: .destructive) {
                    fatalError("HERMES-IOS-U1-DETERMINISTIC-CRASH")
                }
                .accessibilityIdentifier("fixture.crash.trigger")
            }
            .padding(24)
            .navigationTitle("Fixture")
        }
        .accessibilityIdentifier("fixture.root")
        .onAppear {
            UserDefaults.standard.set(challengeResponse, forKey: "HermesChallengeResponse")
            UserDefaults.standard.synchronize()
        }
    }
}
