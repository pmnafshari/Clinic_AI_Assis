// Jarvis - Jarvis and the clinic's records as an app of its own (P28 follow-up, 2026-10-11).
// One window, two tabs: Jarvis (its own state, pause/resume and page - its login service stays the only listener) and
// Clinic (the clinic app on this Mac: sign in, then every record your role allows, through the clinic's own role
// checks, CSRF and audit - no direct database access). Voice reads and writes nothing; answers stay off while J02 is
// not accepted. Built from the same sources as Clinic Workspace, with this file as its entry point.
import AppKit

Settings.fixedProfile = .connected
Settings.connectedWritesByDefault = true

final class AppDelegate: NSObject, NSApplicationDelegate {
    let settings = Settings()
    lazy var demo = DemoBackend(settings: settings)    // never started: this app opens the connected clinic only
    var workspace: WorkspaceWindow?
    var jarvis: JarvisWindow?
    var permissions: PermissionsWindow?
    var selfTest: SelfTest?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        if let script = SelfTest.requestedScript() {
            selfTest = SelfTest(app: self, script: script)
            selfTest?.start()
            return
        }
        openAll()
    }

    func openAll() {
        showJarvis(nil)
        openWorkspace()
        if let j = jarvis?.window, let c = workspace?.window, !(j.tabbedWindows ?? []).contains(c) {
            j.addTabbedWindow(c, ordered: .above)
        }
        jarvis?.window.makeKeyAndOrderFront(nil)
    }

    func openWorkspace() {
        if workspace == nil { workspace = WorkspaceWindow(app: self) }
        workspace?.show()
    }

    @objc func showWorkspace(_ sender: Any?) { openWorkspace() }

    @objc func showJarvis(_ sender: Any?) {
        if jarvis == nil {
            jarvis = JarvisWindow(clinicTab: true)
            jarvis?.openClinicPage = { [weak self] url, front in
                self?.openWorkspace()
                self?.workspace?.load(url)
                if !front { self?.jarvis?.window.makeKeyAndOrderFront(nil) }   // the sign-in link: stay on Jarvis
            }
        }
        jarvis?.show()
    }

    @objc func showPermissions(_ sender: Any?) {
        if permissions == nil { permissions = PermissionsWindow(app: self) }
        permissions?.show()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { openAll() }
        return true
    }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        if workspace?.hasUnsavedForm() == true {
            let alert = NSAlert()
            alert.messageText = "Quit Jarvis?"
            alert.informativeText = "A form on the Clinic page may not have been saved. Anything not saved is lost. " +
                "The clinic keeps everything that was saved. Jarvis itself keeps running."
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        return .terminateNow
    }

    private func buildMenu() {
        let main = NSMenu()
        func menu(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let sub = NSMenu(title: title)
            items.forEach { sub.addItem($0) }
            item.submenu = sub
            main.addItem(item)
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "", _ mods: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }
        menu("Jarvis", [
            item("About Jarvis", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Settings and Permissions…", #selector(showPermissions(_:)), ","),
            .separator(),
            item("Hide Jarvis", #selector(NSApplication.hide(_:)), "h"),
            item("Quit Jarvis (Jarvis keeps listening)", #selector(NSApplication.terminate(_:)), "q"),
        ])
        menu("File", [item("Close Window", #selector(NSWindow.performClose(_:)), "w")])
        menu("Edit", [
            item("Undo", Selector(("undo:")), "z"), item("Redo", Selector(("redo:")), "Z"), .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"), item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"), item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        menu("View", [
            item("Reload Page", #selector(WorkspaceWindow.reloadPage(_:)), "r"),
            item("Zoom In", #selector(WorkspaceWindow.zoomIn(_:)), "+"),
            item("Zoom Out", #selector(WorkspaceWindow.zoomOut(_:)), "-"),
            item("Actual Size", #selector(WorkspaceWindow.zoomReset(_:)), "0"),
        ])
        menu("Window", [
            item("Jarvis", #selector(showJarvis(_:)), "1"),
            item("Clinic", #selector(showWorkspace(_:)), "2"),
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
signal(SIGTERM, SIG_IGN)
let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
terminate.setEventHandler { exit(0) }
terminate.resume()
app.setActivationPolicy(.regular)
app.run()
