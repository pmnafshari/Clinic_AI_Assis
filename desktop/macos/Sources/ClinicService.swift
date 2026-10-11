import AppKit

/// The connected clinic's backend is the existing web app (run.py) - the same process and database the browser
/// version uses. This app is sandboxed: it cannot start that app, and macOS refuses it an unsandboxed login item
/// (SMAppService: "Operation not permitted"). Starting it at login is the separate Clinic Service app's job, on the
/// same disk image. Here the state is only read.
enum ClinicService {
    static let bundleID = "local.clinicdemo.clinic-service"
    static var appURL: URL? { NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) }

    static func describe(answers: Bool) -> String {
        let running = answers ? "the clinic app is running at 127.0.0.1:5000" : "the clinic app is not running"
        guard appURL != nil else {
            return running + "; Clinic Service (starts it at login) is not installed - it is on the same disk image"
        }
        return running + "; starting it at login is switched on or off in the Clinic Service app"
    }

    static func answers(_ then: @escaping (Bool) -> Void) {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:5000/login")!, timeoutInterval: 2)
        req.httpMethod = "HEAD"
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = resp is HTTPURLResponse
            DispatchQueue.main.async { then(ok) }
        }.resume()
    }

    static func open() {
        guard let u = appURL else { return }
        NSWorkspace.shared.openApplication(at: u, configuration: NSWorkspace.OpenConfiguration())
    }
}
