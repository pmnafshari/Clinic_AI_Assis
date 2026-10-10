import Foundation

/// The two environments. They never share data: each has its own backend origin, its own web data store (cookies,
/// cache) and, for the demo, its own database and files inside this app's container.
enum Profile: String {
    case demo, connected

    var title: String {
        switch self {
        case .demo: return "Isolated demo - synthetic data, kept inside this app"
        case .connected: return "Connected clinic - this Mac's clinic service (its own database)"
        }
    }

    var shortTitle: String { self == .demo ? "ISOLATED DEMO" : "CONNECTED CLINIC" }

    /// Loopback only: LAN or remote clinics need their own transport and authentication design (not approved).
    var origin: URL {
        switch self {
        case .demo: return URL(string: "http://127.0.0.1:\(DemoBackend.port)")!
        case .connected: return URL(string: "http://127.0.0.1:5000")!
        }
    }

    /// One persistent web data store per environment, so a demo session cookie can never reach the clinic (macOS 14+).
    var storeID: UUID {
        switch self {
        case .demo: return UUID(uuidString: "6C1F0D5E-2E0B-4C55-9D1E-0A0000000001")!
        case .connected: return UUID(uuidString: "6C1F0D5E-2E0B-4C55-9D1E-0A0000000002")!
        }
    }
}

/// The app's own settings, in its sandbox container's preferences. Nothing here is a secret: sessions live in the web
/// data store; the clinic's credentials live in the clinic backend.
final class Settings {
    private let defaults = UserDefaults.standard

    var profile: Profile? {
        get { defaults.string(forKey: "profile").flatMap(Profile.init(rawValue:)) }
        set { defaults.set(newValue?.rawValue, forKey: "profile") }
    }

    /// The app's own capability layer, on top of the backend's role checks: may this app send writes to that clinic?
    /// The demo allows them by default; the connected clinic only after the owner switches them on.
    func writesAllowed(_ p: Profile) -> Bool {
        defaults.object(forKey: "writes.\(p.rawValue)") as? Bool ?? (p == .demo)
    }

    func setWritesAllowed(_ p: Profile, _ on: Bool) { defaults.set(on, forKey: "writes.\(p.rawValue)") }

    var pageZoom: Double {
        get { defaults.object(forKey: "pageZoom") as? Double ?? 1.0 }
        set { defaults.set(newValue, forKey: "pageZoom") }
    }

    var dataRoot: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("ClinicWorkspace", isDirectory: true)
    }
}
