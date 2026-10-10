import AppKit
import ServiceManagement
import WebKit

/// The installed-app test hook. `ClinicWorkspace --selftest NAME` reads steps from this app's own container
/// (Application Support/ClinicWorkspace/selftest/NAME.json), drives the real workspace window and its page, and writes
/// NAME.result.json and screenshots beside it. Nothing happens without that launch argument (a double-click or the
/// Dock cannot pass one), and only a name is accepted - never a path - so a script can come only from the container.
/// Downloads go to selftest/downloads instead of a save panel while a test runs; the file picker is not automatable
/// and stays a human check.
final class SelfTest {
    static var active = false
    private unowned let app: AppDelegate
    private let steps: [[String: Any]]
    private let dir: URL
    private let name: String
    private var results: [[String: Any]] = []
    private var index = 0

    static func requestedScript() -> String? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--selftest"), i + 1 < args.count else { return nil }
        let name = args[i + 1]
        guard name.range(of: "^[A-Za-z0-9_-]{1,64}$", options: .regularExpression) != nil else { return nil }
        return name
    }

    static func folder(_ settings: Settings) -> URL { settings.dataRoot.appendingPathComponent("selftest", isDirectory: true) }

    init(app: AppDelegate, script: String) {
        self.app = app
        name = script
        dir = Self.folder(app.settings)
        let data = (try? Data(contentsOf: dir.appendingPathComponent("\(script).json"))) ?? Data("[]".utf8)
        steps = (try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]) ?? []
        Self.active = true
    }

    func start() {
        try? FileManager.default.createDirectory(at: dir.appendingPathComponent("shots"), withIntermediateDirectories: true)
        record(["step": "start", "ok": true, "steps": steps.count, "pid": getpid(),
                "home": FileManager.default.homeDirectoryForCurrentUser.path])
        next()
    }

    private func record(_ r: [String: Any]) {
        results.append(r)
        let data = try? JSONSerialization.data(withJSONObject: results, options: [.prettyPrinted, .sortedKeys])
        try? data?.write(to: dir.appendingPathComponent("\(name).result.json"))
    }

    private func next() {
        guard index < steps.count else {
            record(["step": "end", "ok": true])     // a test run always ends the app
            return DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
        }
        let step = steps[index]
        index += 1
        run(step) { [weak self] r in
            var out = r
            out["step"] = step
            self?.record(out)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { self?.next() }
        }
    }

    private var web: WKWebView? { app.workspace?.web }

    private func run(_ step: [String: Any], _ done: @escaping ([String: Any]) -> Void) {
        if let p = step["profile"] as? String, let profile = Profile(rawValue: p) {
            app.openWorkspace()
            app.workspace?.switchProfile(to: profile, confirm: false)
            return done(["ok": true])
        }
        if let on = step["writes"] as? Bool {
            let p = app.workspace?.profile ?? .demo
            app.settings.setWritesAllowed(p, on)
            app.workspace?.applyWriteSetting()
            return done(["ok": true])
        }
        if let path = step["go"] as? String, let origin = app.workspace?.profile?.origin {
            web?.load(URLRequest(url: URL(string: path, relativeTo: origin)!))
            return done(["ok": true])
        }
        if let code = step["wait_js"] as? String {      // an expression, polled until true
            let seconds = (step["timeout"] as? NSNumber)?.doubleValue ?? 30
            return poll(code, until: Date().addingTimeInterval(seconds), tries: 0, done)
        }
        if let code = step["js"] as? String {
            return evaluate(code) { value, error in
                var r: [String: Any] = ["value": value ?? NSNull()]
                if let e = error { r["error"] = e }
                if let want = step["expect"] { r["ok"] = error == nil && Self.same(value, want) } else { r["ok"] = error == nil }
                done(r)
            }
        }
        if let s = step["shot"] as? String, let w = web {
            return w.takeSnapshot(with: nil) { image, _ in
                var ok = false
                if let image = image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    ok = (try? png.write(to: self.dir.appendingPathComponent("shots/\(s).png"))) != nil
                }
                done(["ok": ok])
            }
        }
        if step["reload"] != nil { web?.reload(); return done(["ok": true]) }
        if step["previews"] != nil {
            return done(["ok": true, "urls": ImagePreview.open.map { $0.url.absoluteString },
                         "titles": ImagePreview.open.map { $0.window.title }])
        }
        if step["page"] != nil {                           // what the window shows, read natively (no page script)
            return done(["ok": web != nil, "url": web?.url?.absoluteString ?? NSNull(), "title": web?.title ?? ""])
        }
        if step["clear_store"] != nil, let w = web {        // a test's sign-ins leave nothing behind in this environment
            let store = w.configuration.websiteDataStore
            return store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast) {
                done(["ok": true])
            }
        }
        if let z = step["zoom"] as? Double { web?.pageZoom = z; return done(["ok": true]) }
        if let size = step["resize"] as? [Double], size.count == 2, let w = app.workspace?.window {
            w.setContentSize(NSSize(width: size[0], height: size[1]))
            return done(["ok": true, "size": [w.frame.width, w.frame.height]])
        }
        if let secs = step["sleep"] as? Double {
            return DispatchQueue.main.asyncAfter(deadline: .now() + secs) { done(["ok": true]) }
        }
        if step["state"] != nil {
            let ws = app.workspace
            return done(["ok": true, "profile": ws?.profile?.rawValue ?? NSNull(), "title": ws?.window.title ?? "",
                         "url": ws?.web?.url?.absoluteString ?? NSNull(), "demo": app.demo.state.text,
                         "demo_pid": app.demo.pid ?? -1, "zoom": ws?.web?.pageZoom ?? 0,
                         "writes": ws?.profile.map { app.settings.writesAllowed($0) } ?? false,
                         "banner": ws?.bannerText ?? "", "status": ws?.statusText ?? ""])
        }
        if step["jarvis"] != nil { app.showJarvis(nil); return done(["ok": true]) }
        if step["jarvis_state"] != nil { return done(["ok": app.jarvis != nil, "strip": app.jarvis?.stripText ?? [:]]) }
        if let on = step["jarvis_pause"] as? Bool { app.jarvis?.setPaused(on); return done(["ok": app.jarvis != nil]) }
        if let on = step["login_item"] as? Bool {      // the app's own "open at login" (SMAppService.mainApp)
            do { if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
            catch { return done(["ok": false, "error": error.localizedDescription]) }
            return done(["ok": true])
        }
        if step["permissions"] != nil {                  // the Settings and Permissions rows as they read
            app.showPermissions(nil)
            return DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [self] in
                done(["ok": app.permissions != nil, "rows": app.permissions?.texts ?? [:]])
            }
        }
        if let on = step["clinic_service"] as? Bool {    // the clinic-service login item, folder given by the test
            if let folder = step["folder"] as? String {
                if folder.isEmpty { UserDefaults.standard.removeObject(forKey: "clinicFolder") }
                else { UserDefaults.standard.set(folder, forKey: "clinicFolder") }
                UserDefaults.standard.synchronize()
            }
            do { if on { try ClinicService.service.register() } else { try ClinicService.service.unregister() } }
            catch {
                let e = error as NSError
                return done(["ok": false, "error": e.localizedDescription, "domain": e.domain, "code": e.code,
                             "status": ClinicService.status().rawValue])
            }
            return done(["ok": true, "status": ClinicService.status().rawValue])
        }
        if step["login_state"] != nil { return done(["ok": true, "status": SMAppService.mainApp.status.rawValue]) }
        if step["overflow"] != nil {                     // the page fits its window at the current size and zoom
            return evaluate("return [document.documentElement.scrollWidth, document.documentElement.clientWidth];") { v, e in
                let a = v as? [Int] ?? []
                done(["ok": e == nil && a.count == 2 && a[0] <= a[1] + 1, "widths": a])
            }
        }
        if step["close_jarvis"] != nil { app.jarvis?.window.performClose(nil); return done(["ok": true]) }
        if step["quit"] != nil {
            done(["ok": true])
            return DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
        }
        done(["ok": false, "error": "unknown step"])
    }

    private func evaluate(_ code: String, _ done: @escaping (Any?, String?) -> Void) {
        guard let w = web else { return done(nil, "no page") }
        w.callAsyncJavaScript(code, arguments: [:], in: nil, in: .page) { result in
            switch result {
            case .success(let v): done(v, nil)
            case .failure(let e): done(nil, e.localizedDescription)
            }
        }
    }

    private func poll(_ code: String, until: Date, tries: Int, _ done: @escaping ([String: Any]) -> Void) {
        guard let w = web else { return done(["ok": false, "error": "no page"]) }
        w.evaluateJavaScript(code) { [weak self] v, err in
            let e = err?.localizedDescription
            guard let self = self else { return }
            if let b = v as? Bool, b { return done(["ok": true, "tries": tries + 1]) }
            let url = self.web?.url?.absoluteString ?? "none"
            if Date() > until {
                return done(["ok": false, "value": v ?? NSNull(), "error": e ?? "timed out", "url": url, "tries": tries + 1])
            }
            let progress: [String: Any] = ["tries": tries + 1, "url": url, "error": e ?? "", "demo": self.app.demo.state.text]
            if let d = try? JSONSerialization.data(withJSONObject: progress) {
                try? d.write(to: self.dir.appendingPathComponent("\(self.name).progress.json"))
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.poll(code, until: until, tries: tries + 1, done) }
        }
    }

    static func same(_ a: Any?, _ b: Any) -> Bool {
        if let a = a as? NSNumber, let b = b as? NSNumber { return a == b }
        if let a = a as? String, let b = b as? String { return a == b }
        return false
    }

    static func downloadTarget(_ settings: Settings, _ name: String) -> URL {
        let d = folder(settings).appendingPathComponent("downloads", isDirectory: true)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d.appendingPathComponent(name.replacingOccurrences(of: "/", with: "_"))
    }
}
