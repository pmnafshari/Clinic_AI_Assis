// The login item behind "Clinic service at login": starts the existing clinic app (run.py) from the folder the owner
// chose in Clinic Workspace. It is not sandboxed (it runs the clinic's own code, which owns the database) - a separate
// trust boundary from the app. It never starts a second backend: if 127.0.0.1:5000 already answers, it exits 0 and
// launchd leaves it stopped.
import Foundation

let bundleID = "local.clinicdemo.workspace"
let prefs = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Containers/\(bundleID)/Data/Library/Preferences/\(bundleID).plist")

func log(_ s: String) { FileHandle.standardError.write(Data("clinic-service: \(s)\n".utf8)) }

func answers() -> Bool {
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

guard let data = try? Data(contentsOf: prefs),
      let dict = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
      let folder = dict["clinicFolder"] as? String else {
    log("no clinic folder chosen in Clinic Workspace - nothing started")
    exit(0)
}
if answers() {
    log("something already answers on 127.0.0.1:5000 - not starting a second clinic app")
    exit(0)
}
let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/clinic-workspace-service.log")
FileManager.default.createFile(atPath: logs.path, contents: nil)
if let h = FileHandle(forWritingAtPath: logs.path) {
    h.seekToEndOfFile()
    dup2(h.fileDescriptor, 1)
    dup2(h.fileDescriptor, 2)
}
guard FileManager.default.changeCurrentDirectoryPath(folder) else {
    log("cannot open \(folder) (macOS may need your permission for that folder) - nothing started")
    exit(1)
}
let python = folder + "/.venv/bin/python"
log("starting \(python) run.py in \(folder)")
let args = [python, "run.py"].map { strdup($0) } + [nil]
execv(python, args)
log("could not start \(python): \(String(cString: strerror(errno)))")
exit(1)
