// Clinic Workspace - the installable macOS shell for the clinic web app and Jarvis (P28).
// The shell hosts the clinic's own pages; every read, write, role check and audit stays in the clinic backend.
import AppKit
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate {
    let settings = Settings()
    lazy var demo = DemoBackend(settings: settings)
    var workspace: WorkspaceWindow?
    var jarvis: JarvisWindow?
    var permissions: PermissionsWindow?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        if let script = SelfTest.requestedScript() {      // test hook: only a script inside this app's own container
            SelfTest(app: self, script: script).start()
            return
        }
        openWorkspace()
    }

    func openWorkspace() {
        if workspace == nil {
            workspace = WorkspaceWindow(app: self)
        }
        workspace?.show()
    }

    @objc func showWorkspace(_ sender: Any?) { openWorkspace() }

    @objc func showJarvis(_ sender: Any?) {
        if jarvis == nil { jarvis = JarvisWindow() }
        jarvis?.show()
    }

    @objc func showPermissions(_ sender: Any?) {
        if permissions == nil { permissions = PermissionsWindow(app: self) }
        permissions?.show()
    }

    @objc func useDemo(_ sender: Any?) { workspace?.switchProfile(to: .demo) }
    @objc func useConnected(_ sender: Any?) { workspace?.switchProfile(to: .connected) }

    // Closing the last window keeps the app (and the demo backend it owns) running; Quit stops what it owns only.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { openWorkspace() }
        return true
    }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        if workspace?.hasUnsavedForm() == true {
            let alert = NSAlert()
            alert.messageText = "Quit Clinic Workspace?"
            alert.informativeText = "A form on the page may not have been saved. Anything not saved is lost. " +
                "The clinic backend keeps everything that was saved."
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        demo.stop()            // only the demo backend this app started; never the clinic service, Jarvis or Ollama
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
        menu("Clinic Workspace", [
            item("About Clinic Workspace", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Settings and Permissions…", #selector(showPermissions(_:)), ","),
            .separator(),
            item("Hide Clinic Workspace", #selector(NSApplication.hide(_:)), "h"),
            item("Quit Clinic Workspace", #selector(NSApplication.terminate(_:)), "q"),
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
        menu("Environment", [
            item("Isolated Demo (synthetic data)", #selector(useDemo(_:)), "1", [.command, .option]),
            item("Connected Clinic (this Mac's clinic service)", #selector(useConnected(_:)), "2", [.command, .option]),
        ])
        menu("Jarvis", [item("Open Jarvis Workspace", #selector(showJarvis(_:)), "j", [.command, .shift])])
        menu("Window", [
            item("Clinic Workspace", #selector(showWorkspace(_:)), "0", [.command, .shift]),
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
