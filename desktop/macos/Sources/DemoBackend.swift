import CryptoKit
import Foundation

/// The isolated demo's own clinic backend: the same clinic code and a bundled CPython, started as this app's child so
/// it inherits the App Sandbox - it can read the app bundle and write only inside this app's container. Its data root
/// is seeded once from a verified synthetic snapshot inside the bundle. Exactly one runs: a lock in the data root and a
/// port check refuse a second one.
final class DemoBackend {
    static let port = 5100

    enum State: Equatable {
        case stopped, starting, running, failed(String), crashed(Int32)
        var text: String {
            switch self {
            case .stopped: return "stopped"
            case .starting: return "starting"
            case .running: return "running"
            case .failed(let why): return "could not start: \(why)"
            case .crashed(let code): return "stopped unexpectedly (exit \(code))"
            }
        }
    }

    private(set) var state: State = .stopped { didSet { onChange?(state) } }
    var onChange: ((State) -> Void)?
    private let settings: Settings
    private var process: Process?
    private var lockFD: Int32 = -1
    private var stopping = false

    init(settings: Settings) { self.settings = settings }

    var root: URL { settings.dataRoot.appendingPathComponent("demo", isDirectory: true) }
    private var resources: URL { Bundle.main.resourceURL! }

    /// Start (or keep) the backend; `done` runs on the main queue once it answers or has failed.
    func ensureRunning(_ done: @escaping (State) -> Void) {
        if state == .running, process?.isRunning == true { return done(state) }
        state = .starting
        DispatchQueue.global().async {
            let result = self.start()
            DispatchQueue.main.async {
                self.state = result
                done(result)
            }
        }
    }

    private func start() -> State {
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            guard takeLock() else { return .failed("another copy of Clinic Workspace is running the demo") }
            if Self.answers() { return .failed("port \(Self.port) is already in use by another program") }
            try seedIfNeeded()
            try linkSearchModel()
            let logs = root.appendingPathComponent("logs", isDirectory: true)
            try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
            let log = logs.appendingPathComponent("backend.log")
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
            let out = try FileHandle(forWritingTo: log)
            out.seekToEndOfFile()
            let p = Process()
            p.executableURL = resources.appendingPathComponent("python/bin/python3.12")
            p.arguments = [resources.appendingPathComponent("backend/workspace_backend.py").path, String(Self.port)]
            p.currentDirectoryURL = root
            var env = ProcessInfo.processInfo.environment
            env["PYTHONPATH"] = resources.appendingPathComponent("backend").path
            env["PYTHONDONTWRITEBYTECODE"] = "1"      // the signed bundle is never written
            env["PYTHONNOUSERSITE"] = "1"
            env["CLINIC_WORKSPACE_PROFILE"] = "demo"
            p.environment = env
            p.standardOutput = out
            p.standardError = out
            p.terminationHandler = { [weak self] proc in
                DispatchQueue.main.async {
                    guard let self = self, !self.stopping else { return }
                    self.state = .crashed(proc.terminationStatus)
                }
            }
            try p.run()
            process = p
            for _ in 0..<120 {                         // up to 30 s for the first start
                if !p.isRunning { return .crashed(p.terminationStatus) }
                if Self.answers() { return .running }
                Thread.sleep(forTimeInterval: 0.25)
            }
            return .failed("no answer on port \(Self.port) within 30 s (see the log in Settings and Permissions)")
        } catch {
            return .failed(error.localizedDescription)
        }
    }

    /// Stops only the process this app started.
    func stop() {
        stopping = true
        defer { stopping = false }
        guard let p = process, p.isRunning else { state = .stopped; return }
        p.terminate()
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        process = nil
        state = .stopped
    }

    var pid: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }

    static func answers() -> Bool {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/login")!, timeoutInterval: 1)
        req.httpMethod = "HEAD"
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return ok
    }

    private func takeLock() -> Bool {
        if lockFD >= 0 { return true }
        let fd = open(root.appendingPathComponent(".backend.lock").path, O_CREAT | O_RDWR, 0o600)
        guard fd >= 0, flock(fd, LOCK_EX | LOCK_NB) == 0 else { if fd >= 0 { close(fd) }; return false }
        lockFD = fd
        return true
    }

    /// First run only: the synthetic snapshot, checked file by file against its SHA-256 list, copied into the data
    /// root. An existing demo is never overwritten (its writes are the demo's own records).
    private func seedIfNeeded() throws {
        let marker = root.appendingPathComponent(".seeded")
        if FileManager.default.fileExists(atPath: marker.path) { return }
        let seed = resources.appendingPathComponent("demo-seed", isDirectory: true)
        try Checksums.verify(folder: seed, list: seed.appendingPathComponent("SHA256SUMS"))
        let fm = FileManager.default
        for name in try fm.contentsOfDirectory(atPath: seed.path) where name != "SHA256SUMS" {
            let dest = root.appendingPathComponent(name)
            if fm.fileExists(atPath: dest.path) { continue }
            try fm.copyItem(at: seed.appendingPathComponent(name), to: dest)
        }
        try Data("seeded from the bundle's synthetic snapshot\n".utf8).write(to: marker)
    }

    /// The local search model is bundled (Apache-2.0); chromadb looks for it under HOME, which in the sandbox is this
    /// app's container. A link, so nothing is copied or downloaded.
    private func linkSearchModel() throws {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let dir = home.appendingPathComponent(".cache/chroma/onnx_models/all-MiniLM-L6-v2", isDirectory: true)
        let link = dir.appendingPathComponent("onnx")
        let target = resources.appendingPathComponent("models/all-MiniLM-L6-v2/onnx")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if (try? FileManager.default.destinationOfSymbolicLink(atPath: link.path)) == target.path { return }
        try? FileManager.default.removeItem(at: link)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
    }
}

enum Checksums {
    struct Mismatch: LocalizedError {
        let file: String
        var errorDescription: String? { "the bundled demo snapshot is damaged (\(file)) - reinstall the app" }
    }

    /// `list` holds "<sha256>  <relative path>" lines (shasum -a 256 format).
    static func verify(folder: URL, list: URL) throws {
        let text = try String(contentsOf: list, encoding: .utf8)
        for line in text.split(separator: "\n") where !line.isEmpty {
            let parts = line.split(separator: " ", maxSplits: 1)
            guard parts.count == 2 else { throw Mismatch(file: String(line)) }
            let rel = parts[1].trimmingCharacters(in: .whitespaces)
            let url = folder.appendingPathComponent(rel)
            guard let digest = sha256(url), digest == parts[0] else { throw Mismatch(file: rel) }
        }
    }

    static func sha256(_ url: URL) -> String? {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return nil }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
