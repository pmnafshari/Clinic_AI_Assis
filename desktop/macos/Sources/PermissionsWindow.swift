import AVFoundation
import AppKit
import ServiceManagement
import UserNotifications

/// What this app may do, and what macOS, the clinic and Jarvis currently allow. States are read, never assumed:
/// "not requested", "allowed", "denied", "requires System Settings", "unavailable". Nothing here can grant a macOS
/// permission silently - the buttons ask macOS at the point of use or open System Settings.
final class PermissionsWindow: NSObject {
    private unowned let app: AppDelegate
    let window: NSWindow
    private let grid = NSGridView()
    private var rows: [String: (NSTextField, NSButton?)] = [:]

    init(app: AppDelegate) {
        self.app = app
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 860, height: 560),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        super.init()
        window.title = "Settings and Permissions"
        window.isReleasedWhenClosed = false
        grid.rowSpacing = 10
        grid.columnSpacing = 14
        let scroll = NSScrollView()
        scroll.documentView = grid
        scroll.hasVerticalScroller = true
        window.contentView = scroll
        addRow("environment", "Environment", nil)
        addRow("session", "Clinic access (this environment)", nil)
        addRow("writes", "Writes from this app", button("Switch", #selector(toggleWrites)))
        addRow("mic", "Microphone (this app)", nil)
        addRow("jarvis", "Microphone and listening (Jarvis)", button("Open Jarvis", #selector(openJarvis)))
        addRow("files", "Files and folders", nil)
        addRow("notify", "Notifications", button("Ask macOS", #selector(askNotifications)))
        addRow("login", "Open Clinic Workspace at login", button("Switch", #selector(toggleLogin)))
        addRow("service", "Clinic service at login (connected clinic)", button("Switch", #selector(toggleService)))
        addRow("demo", "Isolated demo backend", button("Restart", #selector(restartDemo)))
        addRow("network", "Network", nil)
        grid.translatesAutoresizingMaskIntoConstraints = false
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        return b
    }

    private func addRow(_ key: String, _ title: String, _ b: NSButton?) {
        let name = NSTextField(labelWithString: title)
        name.font = .systemFont(ofSize: 13, weight: .semibold)
        let state = NSTextField(wrappingLabelWithString: "...")
        state.preferredMaxLayoutWidth = 520
        state.isSelectable = true
        grid.addRow(with: [name, state, b ?? NSView()])
        rows[key] = (state, b)
    }

    private func set(_ key: String, _ text: String, button: String? = nil) {
        rows[key]?.0.stringValue = text
        if let t = button { rows[key]?.1?.title = t }
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        refresh()
    }

    func refresh() {
        let p = app.workspace?.profile ?? app.settings.profile ?? .demo
        set("environment", "\(p.shortTitle): \(p.title). Switch from the Environment menu.")
        set("writes", app.settings.writesAllowed(p)
            ? "allowed - the clinic's own role checks still decide every write"
            : "switched off for this environment - the app refuses the page's writes; reading still works",
            button: app.settings.writesAllowed(p) ? "Switch off" : "Allow writes")
        set("files", "one file at a time, only the file or folder you pick (macOS file picker); nothing is " +
                     "remembered between picks, and no other folder is readable by this app")
        set("network", "this app talks only to this Mac (127.0.0.1): the clinic, its own demo backend and Jarvis; " +
                       "other links open in your browser")
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .notDetermined: set("mic", "not requested - this app never records; Jarvis's own service listens")
        case .authorized: set("mic", "allowed for this app (it still never records)")
        case .denied: set("mic", "denied for this app - nothing changes: it never records")
        case .restricted: set("mic", "unavailable (restricted by this Mac's settings)")
        @unknown default: set("mic", "unknown")
        }
        loginState()
        serviceState()
        demoState()
        sessionState()
        jarvisState()
        UNUserNotificationCenter.current().getNotificationSettings { s in
            let text: String
            switch s.authorizationStatus {
            case .notDetermined: text = "not requested - used only to tell you when the demo backend stops"
            case .authorized, .provisional: text = "allowed"
            case .denied: text = "denied - requires System Settings > Notifications to change; status stays in the window"
            default: text = "unavailable"
            }
            DispatchQueue.main.async { self.set("notify", text) }
        }
    }

    private func loginState() {
        let s = SMAppService.mainApp.status
        let text: String
        switch s {
        case .enabled: text = "on"
        case .notRegistered: text = "off"
        case .requiresApproval: text = "waiting for your approval in System Settings > General > Login Items"
        case .notFound: text = "unavailable for this copy of the app (install it in Applications first)"
        @unknown default: text = "unknown"
        }
        set("login", text, button: s == .enabled ? "Switch off" : "Switch on")
    }

    private func serviceState() {
        let s = ClinicService.status()
        set("service", ClinicService.describe(s), button: s == .enabled ? "Switch off" : "Switch on")
    }

    private func demoState() {
        let d = app.demo
        set("demo", "\(d.state.text)\(d.pid.map { " (process \($0))" } ?? "") - data only in this app's container: " +
                    "\(d.root.path)")
    }

    private func sessionState() {
        set("session", "checking...")
        app.workspace?.session { [weak self] text in self?.set("session", text) }
    }

    private func jarvisState() {
        URLSession.shared.dataTask(with: URLRequest(url: JarvisWindow.origin.appendingPathComponent("status"),
                                                    timeoutInterval: 2)) { [weak self] data, _, _ in
            let s = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let text: String
            if let s = s {
                let state = s["state"] as? String ?? "?", reason = s["reason"] as? String ?? ""
                text = "\(state) - \(reason). Jarvis's own login service holds the microphone permission (macOS lists " +
                       "it under its own name); change it in System Settings > Privacy & Security > Microphone. " +
                       "Pause it from the Jarvis window."
            } else {
                text = "Jarvis is not running - its own login service starts it; this app never listens"
            }
            DispatchQueue.main.async { self?.set("jarvis", text) }
        }.resume()
    }

    // MARK: actions

    @objc private func toggleWrites() {
        let p = app.workspace?.profile ?? .demo
        let on = !app.settings.writesAllowed(p)
        if on, p == .connected {
            let alert = NSAlert()
            alert.messageText = "Allow writes to the connected clinic from this app?"
            alert.informativeText = "Saving here changes the clinic's own database, the same one the browser version " +
                "uses. Your role still decides what you may change, and the page reloads."
            alert.addButton(withTitle: "Allow writes")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        app.settings.setWritesAllowed(p, on)
        app.workspace?.applyWriteSetting()
        refresh()
    }

    @objc private func openJarvis() { app.showJarvis(nil) }

    @objc private func askNotifications() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in
            DispatchQueue.main.async { self.refresh() }
        }
    }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            set("login", "macOS refused: \(error.localizedDescription)")
            return
        }
        loginState()
    }

    @objc private func toggleService() {
        let result = ClinicService.toggle(window: window)
        if let r = result { set("service", r) } else { serviceState() }
    }

    @objc private func restartDemo() {
        app.demo.stop()
        app.demo.ensureRunning { [weak self] _ in self?.demoState() }
        demoState()
    }
}
