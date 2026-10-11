// Clinic Service: switches on or off the login item that starts this Mac's clinic app (run.py, the same app and
// database the browser uses) at login. Not sandboxed, by design - see ClinicServiceHelper.swift. Clinic Workspace only
// reads whether the clinic app answers; it cannot start or stop it.
//
// For the installed-app test: ClinicService --status | --on <folder> | --off   (prints JSON, changes nothing else)
import AppKit
import ServiceManagement

let agent = SMAppService.agent(plistName: "local.clinicdemo.clinic-service.plist")
let prefs = UserDefaults.standard

func clinicAnswers() -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(5000).bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    return withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    } == 0
}

func isClinicFolder(_ path: String) -> Bool {
    let fm = FileManager.default
    return fm.fileExists(atPath: path + "/run.py") && fm.fileExists(atPath: path + "/.venv/bin/python")
}

func describe(_ s: SMAppService.Status) -> String {
    switch s {
    case .enabled: return "on - starts the clinic app at login, unless one is already running"
    case .requiresApproval: return "waiting for your approval in System Settings > General > Login Items"
    case .notRegistered, .notFound: return "off"     // macOS says "not found" for an item never switched on
    @unknown default: return "unknown"
    }
}

/// -> nil when done, else what went wrong
func switchOn(_ folder: String) -> String? {
    guard isClinicFolder(folder) else { return "that folder has no run.py and .venv - nothing was changed" }
    prefs.set(folder, forKey: "clinicFolder")
    prefs.synchronize()
    do { try agent.register() } catch { return "macOS refused: \(error.localizedDescription)" }
    return nil
}

func switchOff() -> String? {
    do { try agent.unregister() } catch { return "macOS refused: \(error.localizedDescription)" }
    return nil
}

func status() -> [String: Any] {
    ["login_item": describe(agent.status), "status": agent.status.rawValue,
     "folder": prefs.string(forKey: "clinicFolder") ?? "", "clinic_answers": clinicAnswers()]
}

func printJSON(_ d: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: d, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

let args = CommandLine.arguments
if args.count > 1 {
    var error: String? = nil
    switch args[1] {
    case "--on" where args.count > 2: error = switchOn(args[2])
    case "--off": error = switchOff()
    case "--status": break
    default: print("usage: ClinicService --status | --on <folder> | --off"); exit(2)
    }
    var d = status()
    if let e = error { d["error"] = e }
    printJSON(d)
    exit(error == nil ? 0 : 1)
}

final class Controller: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let folder = NSTextField(wrappingLabelWithString: "")
    let item = NSTextField(wrappingLabelWithString: "")
    let clinic = NSTextField(wrappingLabelWithString: "")
    let message = NSTextField(wrappingLabelWithString: "")
    let toggle = NSButton(title: "Switch on", target: nil, action: nil)
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        appItem.submenu = NSMenu()
        appItem.submenu?.addItem(withTitle: "Quit Clinic Service", action: #selector(NSApplication.terminate(_:)),
                                 keyEquivalent: "q")
        menu.addItem(appItem)
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 300),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Clinic Service"
        let intro = NSTextField(wrappingLabelWithString:
            "Starts this Mac's clinic app (the one the browser and Clinic Workspace's connected clinic use) when you " +
            "log in. It never starts a second one. Switching it off stops the clinic app it started.")
        let choose = NSButton(title: "Choose clinic folder...", target: self, action: #selector(chooseFolder))
        toggle.target = self
        toggle.action = #selector(flip)
        message.textColor = .systemRed
        let grid = NSGridView(views: [
            [label("Clinic folder"), folder], [label("Start at login"), item], [label("Clinic app"), clinic]])
        grid.rowSpacing = 10
        grid.column(at: 1).width = 440
        let buttons = NSStackView(views: [choose, toggle])
        let stack = NSStackView(views: [intro, grid, buttons, message])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        window.contentView = stack
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func label(_ s: String) -> NSTextField {
        let l = NSTextField(labelWithString: s)
        l.font = .systemFont(ofSize: 13, weight: .semibold)
        return l
    }

    func refresh() {
        let s = status()
        folder.stringValue = (s["folder"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "not chosen"
        item.stringValue = s["login_item"] as? String ?? ""
        clinic.stringValue = (s["clinic_answers"] as? Bool ?? false)
            ? "running at http://127.0.0.1:5000" : "not running"
        toggle.title = agent.status == .enabled ? "Switch off" : "Switch on"
    }

    @objc func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.message = "Choose the clinic folder (the one with run.py and .venv)"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if isClinicFolder(url.path) {
            prefs.set(url.path, forKey: "clinicFolder")
            message.stringValue = ""
        } else {
            message.stringValue = "That folder has no run.py and .venv - nothing was changed."
        }
        refresh()
    }

    @objc func flip() {
        if agent.status == .enabled {
            message.stringValue = switchOff() ?? ""
        } else if let f = prefs.string(forKey: "clinicFolder") {
            message.stringValue = switchOn(f) ?? ""
            if agent.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
        } else {
            message.stringValue = "Choose the clinic folder first."
        }
        refresh()
    }
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
app.setActivationPolicy(.regular)
app.run()
