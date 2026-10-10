import AppKit
import WebKit

/// The clinic's own pages, for one environment at a time. The page gets no bridge into the app: no script message
/// handler, navigation only within the selected clinic's origin, everything else opened in the default browser.
final class WorkspaceWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private unowned let app: AppDelegate
    let window: NSWindow
    private let banner = NSTextField(labelWithString: "")
    private let status = NSTextField(labelWithString: "")
    private let stack = NSStackView()
    private(set) var web: WKWebView?
    private(set) var profile: Profile?
    private var dirty = false
    private var dirtyTimer: Timer?
    private var notice: String?

    init(app: AppDelegate) {
        self.app = app
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init()
        window.title = "Clinic Workspace"
        window.minSize = NSSize(width: 480, height: 420)
        window.setFrameAutosaveName("ClinicWorkspaceMain")
        window.isReleasedWhenClosed = false
        window.delegate = self
        stack.orientation = .vertical
        stack.spacing = 0
        stack.alignment = .leading
        for label in [banner, status] {
            label.font = .systemFont(ofSize: 13, weight: label === banner ? .semibold : .regular)
            label.lineBreakMode = .byTruncatingTail
            label.drawsBackground = true
            label.isSelectable = true
            label.setAccessibilityRole(.staticText)
        }
        banner.setAccessibilityLabel("Environment")
        status.setAccessibilityLabel("Connection status")
        stack.addArrangedSubview(banner)
        stack.addArrangedSubview(status)
        window.contentView = stack
        app.demo.onChange = { [weak self] _ in self?.refreshStatus() }
        dirtyTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.pollDirty() }
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if profile == nil {
            switchProfile(to: app.settings.profile ?? chooseProfile(), confirm: false)
        }
    }

    // MARK: environment

    private func chooseProfile() -> Profile {
        let alert = NSAlert()
        alert.messageText = "Which clinic should this workspace open?"
        alert.informativeText = "Isolated demo: synthetic patients and its own database, inside this app only - nothing " +
            "you do there reaches the clinic.\n\nConnected clinic: the clinic service already running on this Mac " +
            "(127.0.0.1:5000) and its database, the same one the browser version uses. You sign in as yourself; " +
            "writes from this app stay off until you switch them on in Settings and Permissions.\n\nYou can switch " +
            "later from the Environment menu."
        alert.addButton(withTitle: "Isolated demo")
        alert.addButton(withTitle: "Connected clinic")
        return alert.runModal() == .alertFirstButtonReturn ? .demo : .connected
    }

    func switchProfile(to next: Profile, confirm: Bool = true) {
        if confirm, next == profile { return }
        if confirm, dirty {
            let alert = NSAlert()
            alert.messageText = "Switch to \(next == .demo ? "the isolated demo" : "the connected clinic")?"
            alert.informativeText = "A form on this page has changes that were not saved; they will be lost."
            alert.addButton(withTitle: "Switch")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        profile = next
        app.settings.profile = next
        dirty = false
        makeWebView(for: next)
        open(next)
    }

    private func makeWebView(for p: Profile) {
        web?.removeFromSuperview()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = WKWebsiteDataStore(forIdentifier: p.storeID)
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        config.userContentController.addUserScript(WKUserScript(source: Self.pageScript(writes: app.settings.writesAllowed(p)),
                                                                injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let w = WKWebView(frame: .zero, configuration: config)
        w.navigationDelegate = self
        w.uiDelegate = self
        w.pageZoom = app.settings.pageZoom
        w.allowsBackForwardNavigationGestures = true
        w.translatesAutoresizingMaskIntoConstraints = false
        w.setAccessibilityLabel("Clinic page")
        stack.addArrangedSubview(w)
        w.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        banner.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        status.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        web = w
        refreshStatus()
    }

    func open(_ p: Profile) {
        refreshStatus()
        switch p {
        case .demo:
            showMessage("Starting the isolated demo", "Starting this app's own synthetic clinic backend...")
            app.demo.ensureRunning { [weak self] state in
                guard let self = self, self.profile == .demo else { return }
                if state == .running { self.web?.load(URLRequest(url: p.origin.appendingPathComponent("login"))) }
                else { self.showMessage("The isolated demo could not start", state.text, retry: true) }
            }
        case .connected:
            DispatchQueue.global().async {
                let up = Self.answers(p.origin)
                DispatchQueue.main.async {
                    guard self.profile == .connected else { return }
                    if up { self.web?.load(URLRequest(url: p.origin)) }
                    else { self.showMessage("The clinic service is not running", Self.notRunning, retry: true) }
                }
            }
        }
    }

    static let notRunning = "Nothing answers at 127.0.0.1:5000, so nothing was read or written. Start the clinic " +
        "service (Settings and Permissions > Clinic service at login), or start the browser version as before; this " +
        "app connects to whichever one is running and never starts a second one."

    static func answers(_ origin: URL) -> Bool {
        var req = URLRequest(url: origin.appendingPathComponent("login"), timeoutInterval: 2)
        req.httpMethod = "HEAD"
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, r, _ in
            ok = (r as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 3)
        return ok
    }

    func refreshStatus() {
        guard let p = profile else { return }
        banner.stringValue = "  \(p.shortTitle)  -  \(p.title)"
        banner.backgroundColor = p == .demo ? NSColor(srgbRed: 0.84, green: 0.94, blue: 0.92, alpha: 1)
                                            : NSColor(srgbRed: 0.99, green: 0.91, blue: 0.80, alpha: 1)
        banner.textColor = NSColor(srgbRed: 0.08, green: 0.13, blue: 0.21, alpha: 1)
        var parts = ["Clinic: \(p.origin.absoluteString)"]
        if p == .demo { parts.append("demo backend \(app.demo.state.text)") }
        parts.append("writes from this app: \(app.settings.writesAllowed(p) ? "allowed" : "switched off")")
        if let n = notice { parts.append(n) }
        status.stringValue = "  " + parts.joined(separator: "   |   ")
        status.backgroundColor = .windowBackgroundColor
        status.textColor = .labelColor
        window.title = "Clinic Workspace - \(p == .demo ? "Isolated demo" : "Connected clinic")"
    }

    private func showMessage(_ title: String, _ text: String, retry: Bool = false) {
        let esc = { (s: String) in s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;") }
        let button = retry ? "<p><a href=\"workspace-retry:\">Try again</a></p>" : ""
        let html = """
        <!doctype html><meta charset="utf-8"><title>\(esc(title))</title>
        <body style="font:16px -apple-system;margin:48px;color:#142036;max-width:44rem">
        <h1 role="status" style="font-size:22px">\(esc(title))</h1><p>\(esc(text))</p>\(button)</body>
        """
        web?.loadHTMLString(html, baseURL: nil)
    }

    // MARK: menu actions

    @objc func reloadPage(_ sender: Any?) { if let p = profile { if web?.url?.scheme == "http" { web?.reload() } else { open(p) } } }
    @objc func zoomIn(_ sender: Any?) { setZoom((web?.pageZoom ?? 1) + 0.1) }
    @objc func zoomOut(_ sender: Any?) { setZoom((web?.pageZoom ?? 1) - 0.1) }
    @objc func zoomReset(_ sender: Any?) { setZoom(1) }
    private func setZoom(_ z: Double) {
        let v = min(max(z, 0.5), 3.0)
        web?.pageZoom = v
        app.settings.pageZoom = v
    }

    func applyWriteSetting() { if let p = profile { makeWebView(for: p); open(p) } }

    // MARK: unsaved forms (the page is asked; it cannot call the app)

    private func pollDirty() {
        web?.evaluateJavaScript("window.__workspaceDirty === true") { [weak self] v, _ in self?.dirty = (v as? Bool) ?? false }
    }

    func hasUnsavedForm() -> Bool { dirty }

    var bannerText: String { banner.stringValue }
    var statusText: String { status.stringValue }

    /// Who is signed in here, asked of the clinic itself (`/api/session`, under the page's own sign-in).
    func session(_ done: @escaping (String) -> Void) {
        guard let w = web, sameOrigin(w.url) else { return done("not connected to a clinic page") }
        let code = """
            const r = await fetch("/api/session", {credentials: "same-origin", redirect: "manual"});
            if (r.status !== 200) return null;
            return await r.json();
            """
        w.callAsyncJavaScript(code, arguments: [:], in: nil, in: .page) { result in
            guard case .success(let v) = result, let d = v as? [String: Any], let user = d["username"] as? String else {
                return done("not signed in - sign in on the page; the clinic decides what each role may see and do")
            }
            let caps = (d["capabilities"] as? [String]) ?? []
            done("signed in as \(user) (\(d["role"] as? String ?? "?")); the clinic allows: " +
                 (caps.isEmpty ? "reading only" : caps.joined(separator: ", ")))
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { true }       // closing keeps the app; Quit asks

    /// Tracks edited forms; with writes switched off it also refuses the page's own write requests (the clinic's
    /// role checks still decide everything else - this is the app's extra capability layer, not a security boundary).
    static func pageScript(writes: Bool) -> String {
        var s = """
        (function () {
          document.addEventListener("input", function (e) { if (e.target.form) window.__workspaceDirty = true; }, true);
          document.addEventListener("submit", function () { window.__workspaceDirty = false; }, true);
        """
        if !writes {
            s += """
              const ok = (m) => !m || ["GET", "HEAD"].includes(String(m).toUpperCase());
              const f = window.fetch;
              window.fetch = function (input, init) {
                if (!ok((init && init.method) || (input && input.method))) return Promise.reject(new Error("writes are switched off in Clinic Workspace"));
                return f.apply(this, arguments);
              };
              const o = XMLHttpRequest.prototype.open;
              XMLHttpRequest.prototype.open = function (m) {
                if (!ok(m)) throw new Error("writes are switched off in Clinic Workspace");
                return o.apply(this, arguments);
              };
            """
        }
        return s + "})();"
    }

    // MARK: navigation policy

    private func sameOrigin(_ url: URL?) -> Bool {
        guard let url = url, let o = profile?.origin else { return false }
        return url.scheme == o.scheme && url.host == o.host && url.port == o.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.scheme == "workspace-retry" { if let p = profile { open(p) }; return decisionHandler(.cancel) }
        if url.scheme == "about" { return decisionHandler(.allow) }
        if sameOrigin(url) {
            let method = action.request.httpMethod ?? "GET"
            if method != "GET", let p = profile, !app.settings.writesAllowed(p) {
                notice = "a write was refused: writes from this app are switched off for this clinic"
                refreshStatus()
                NSSound.beep()
                return decisionHandler(.cancel)
            }
            notice = nil
            refreshStatus()
            return decisionHandler(action.shouldPerformDownload ? .download : .allow)
        }
        if ["http", "https", "mailto"].contains(url.scheme ?? "") && action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)               // other sites, the patient portal, the public site
        }
        decisionHandler(.cancel)                       // file:, javascript: and anything else never load here
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if !response.canShowMIMEType || disposition.lowercased().hasPrefix("attachment") {
            return decisionHandler(.download)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        if SelfTest.active { return completionHandler(SelfTest.downloadTarget(app.settings, suggestedFilename)) }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.message = "Save the file from the clinic - it is saved only where you choose."
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.url : nil) }
    }

    func downloadDidFinish(_ download: WKDownload) {
        notice = "download saved"
        refreshStatus()
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        notice = "download failed: \(error.localizedDescription)"
        refreshStatus()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // signed out or expired: the clinic shows its sign-in page; drop cached pages of this environment
        if sameOrigin(webView.url), webView.url?.path == "/login" {
            webView.configuration.websiteDataStore.removeData(
                ofTypes: [WKWebsiteDataTypeDiskCache, WKWebsiteDataTypeMemoryCache], modifiedSince: .distantPast) {}
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }

    private func failed(_ error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
        if e.domain == "WebKitErrorDomain" && e.code == 102 { return }       // frame load interrupted (a download)
        guard let p = profile else { return }
        let text = "The clinic did not answer (\(e.localizedDescription)). Nothing was sent again automatically - " +
            "if you were saving something, check the page after the clinic is back before saving again."
        showMessage(p == .demo ? "The isolated demo is not answering" : "The clinic service is not answering", text, retry: true)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        showMessage("The page stopped", "The page's process ended. Nothing was resent.", retry: true)
    }

    // MARK: UI delegate: new windows, file picker, dialogs

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if sameOrigin(url) { webView.load(action.request) } else if ["http", "https"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.message = "Choose the file to send to the clinic - only the file you choose is read."
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }
}
