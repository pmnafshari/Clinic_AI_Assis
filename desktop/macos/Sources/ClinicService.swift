import AppKit
import ServiceManagement

/// The connected clinic's backend is the existing web app in the clinic folder - the same process and database the
/// browser version uses. This app cannot start it from inside its sandbox, so it registers a login item (with the
/// owner's approval in System Settings) whose small helper starts `run.py` from the chosen clinic folder - and does
/// nothing when something already answers on 5000, so there is never a second backend.
enum ClinicService {
    static let plist = "local.clinicdemo.workspace.clinic-service.plist"
    static var service: SMAppService { SMAppService.agent(plistName: plist) }

    static func status() -> SMAppService.Status { service.status }

    static func describe(_ s: SMAppService.Status) -> String {
        let folder = UserDefaults.standard.string(forKey: "clinicFolder") ?? "not chosen"
        switch s {
        case .enabled: return "on - starts the clinic app from \(folder) at login, unless one is already running"
        case .notRegistered: return "off - the connected clinic works whenever the clinic app is running (browser " +
                                    "version as before, or this login item)"
        case .requiresApproval: return "waiting for your approval in System Settings > General > Login Items"
        case .notFound: return "unavailable for this copy of the app"
        @unknown default: return "unknown"
        }
    }

    /// -> a message to show, or nil when the state should simply be re-read.
    static func toggle(window: NSWindow) -> String? {
        do {
            if status() == .enabled {
                try service.unregister()      // launchd stops the helper's own process only
                return nil
            }
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.message = "Choose the clinic folder (the one with run.py and .venv) - only its path is kept."
            guard panel.runModal() == .OK, let url = panel.url else { return nil }
            let fm = FileManager.default
            guard fm.fileExists(atPath: url.appendingPathComponent("run.py").path),
                  fm.fileExists(atPath: url.appendingPathComponent(".venv/bin/python").path) else {
                return "that folder has no run.py and .venv - nothing was changed"
            }
            UserDefaults.standard.set(url.path, forKey: "clinicFolder")
            UserDefaults.standard.synchronize()
            try service.register()
            if status() == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
            return nil
        } catch {
            return "macOS refused: \(error.localizedDescription)"
        }
    }
}
