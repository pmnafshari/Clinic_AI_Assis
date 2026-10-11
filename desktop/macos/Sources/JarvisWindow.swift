import AppKit
import WebKit

/// Jarvis's own page in a window of its own. Jarvis keeps its own lifecycle: its login service is the only listener
/// on this Mac; this app never opens the microphone. Closing this window changes nothing; Pause asks Jarvis itself to
/// close the microphone until Resume.
final class JarvisWindow: NSObject, WKNavigationDelegate, WKUIDelegate {
    static let origin = URL(string: "http://127.0.0.1:5020")!
    let window: NSWindow
    private let web: WKWebView
    private let listening = NSTextField(labelWithString: "Listening: checking...")
    private let answers = NSTextField(labelWithString: "")
    private let actions = NSTextField(labelWithString: "Actions by voice: none - not built (J03 not started); voice " +
                                                       "is never proof of identity")
    private let pause = NSButton(title: "Pause listening", target: nil, action: nil)
    private var timer: Timer?
    private var paused = false

    override init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        let config = WKWebViewConfiguration()
        config.websiteDataStore = WKWebsiteDataStore(forIdentifier: UUID(uuidString: "6C1F0D5E-2E0B-4C55-9D1E-0A0000000003")!)
        web = WKWebView(frame: .zero, configuration: config)
        super.init()
        window.title = "Jarvis"
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ClinicWorkspaceJarvis")
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setAccessibilityLabel("Jarvis page")
        pause.target = self
        pause.action = #selector(togglePause(_:))
        let strip = NSStackView(views: [listening, answers, actions, pause])
        strip.orientation = .vertical
        strip.alignment = .leading
        strip.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        for l in [listening, answers, actions] { l.lineBreakMode = .byTruncatingTail; l.isSelectable = true }
        listening.font = .systemFont(ofSize: 13, weight: .semibold)
        let stack = NSStackView(views: [strip, web])
        stack.orientation = .vertical
        stack.spacing = 0
        web.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = stack
        web.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        web.load(URLRequest(url: Self.origin))
        poll()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func poll() {
        guard window.isVisible else { return }
        URLSession.shared.dataTask(with: URLRequest(url: Self.origin.appendingPathComponent("status"), timeoutInterval: 2)) {
            [weak self] data, _, _ in
            let s = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { self?.render(s) }
        }.resume()
    }

    private func render(_ s: [String: Any]?) {
        guard let s = s else {
            listening.stringValue = "Listening: Jarvis is not running (it starts at login on its own; this app does not start it)"
            answers.stringValue = ""
            pause.isEnabled = false
            return
        }
        let state = s["state"] as? String ?? "?"
        let reason = s["reason"] as? String ?? ""
        paused = s["paused"] as? Bool ?? false
        listening.stringValue = "Listening: \(state) - \(reason)"
        let a = s["answers"] as? [String: Any]
        answers.stringValue = "Clinic-guide answers: \(a?["detail"] as? String ?? "unknown")"
        pause.isEnabled = s["paused"] != nil              // an older Jarvis without pause support
        pause.title = paused ? "Resume listening" : "Pause listening"
    }

    /// The strip as a person reads it (for the installed-app test).
    var stripText: [String: Any] {
        ["listening": listening.stringValue, "answers": answers.stringValue, "actions": actions.stringValue,
         "pause_button": pause.title, "pause_enabled": pause.isEnabled, "page": web.url?.absoluteString ?? ""]
    }

    func setPaused(_ on: Bool) { if on != paused { togglePause(nil) } }

    @objc private func togglePause(_ sender: Any?) {
        var req = URLRequest(url: Self.origin.appendingPathComponent("listening"), timeoutInterval: 3)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(Self.origin.absoluteString, forHTTPHeaderField: "Origin")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["paused": !paused])
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 204
            DispatchQueue.main.async {
                if !ok { self?.listening.stringValue = "Listening: Jarvis did not take the request - nothing changed" }
                self?.poll()
            }
        }.resume()
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.host == Self.origin.host && url.port == Self.origin.port { return decisionHandler(.allow) }
        if ["http", "https"].contains(url.scheme ?? "") && action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)               // a cited page opens in the clinic app's browser session
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        webView.loadHTMLString("""
            <!doctype html><body style="font:16px -apple-system;margin:40px"><h1 role="status" style="font-size:20px">
            Jarvis is not running</h1><p>Its own login service starts it; this app only shows it.</p></body>
            """, baseURL: nil)
    }

    /// "Open page N ... (opens the clinic app in a new tab)": the default browser, under its own clinic sign-in.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        return nil
    }
}
