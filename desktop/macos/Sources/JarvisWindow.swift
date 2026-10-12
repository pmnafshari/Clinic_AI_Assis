import AppKit
import WebKit

/// Jarvis's own page in a window of its own. Jarvis keeps its own lifecycle: its login service is the only listener
/// on this Mac; this app never opens the microphone. Closing this window changes nothing. Listening is switched on and
/// off with the switch in the middle of the page's orb (owner, 2026-10-11), which asks Jarvis itself.
final class JarvisWindow: NSObject, WKNavigationDelegate, WKUIDelegate {
    static let origin = URL(string: "http://127.0.0.1:5020")!
    let window: NSWindow
    private let web: WKWebView
    /// Set in Jarvis.app: a clinic page opens in its own Clinic tab instead of the browser - brought to the front
    /// (a record), or not (the one-time sign-in link, 2026-10-12).
    var openClinicPage: ((URL, Bool) -> Void)?

    private func isClinic(_ url: URL) -> Bool { url.host == "127.0.0.1" && url.port == 5000 }

    /// `clinicTab`: Jarvis.app, whose page may open clinic pages in the Clinic tab by itself; the page learns it from
    /// the user agent ("JarvisApp") - there is no bridge between the page and the app.
    init(clinicTab: Bool = false) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        let config = WKWebViewConfiguration()
        if clinicTab { config.applicationNameForUserAgent = "JarvisApp" }
        config.websiteDataStore = WKWebsiteDataStore(forIdentifier: UUID(uuidString: "6C1F0D5E-2E0B-4C55-9D1E-0A0000000003")!)
        web = WKWebView(frame: .zero, configuration: config)
        super.init()
        window.title = "Jarvis"
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ClinicWorkspaceJarvis")
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setAccessibilityLabel("Jarvis page")
        window.contentView = web
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        web.load(URLRequest(url: Self.origin))
    }

    /// Jarvis's own status, as its page reads it (for the installed-app test).
    func status(_ done: @escaping ([String: Any]) -> Void) {
        let page = web.url?.absoluteString ?? ""
        URLSession.shared.dataTask(with: URLRequest(url: Self.origin.appendingPathComponent("status"), timeoutInterval: 2)) {
            data, _, _ in
            let s = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
            DispatchQueue.main.async { done(["page": page, "status": s]) }
        }.resume()
    }

    /// Run a script in Jarvis's page, as a person would act on it (the installed-app test only).
    func evaluate(_ code: String, _ done: @escaping (Any?, String?) -> Void) {
        web.callAsyncJavaScript(code, arguments: [:], in: nil, in: .page) { result in
            switch result {
            case .success(let v): done(v, nil)
            case .failure(let e): done(nil, e.localizedDescription)
            }
        }
    }

    /// Pause or resume listening, as the page's switch does (for the installed-app test).
    func setPaused(_ on: Bool) {
        var req = URLRequest(url: Self.origin.appendingPathComponent("listening"), timeoutInterval: 3)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(Self.origin.absoluteString, forHTTPHeaderField: "Origin")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["paused": on])
        URLSession.shared.dataTask(with: req).resume()
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.host == Self.origin.host && url.port == Self.origin.port { return decisionHandler(.allow) }
        if isClinic(url), let open = openClinicPage, action.targetFrame?.isMainFrame != false {
            // a record the page opens by itself, or the one-time sign-in link: always in the Clinic tab, never here
            open(url, !url.path.hasPrefix("/jarvis/handoff/"))
        } else if ["http", "https"].contains(url.scheme ?? "") && action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)              // a cited page opens in the clinic app's browser session
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
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? "") {
            if isClinic(url), let open = openClinicPage { open(url, !url.path.hasPrefix("/jarvis/handoff/")) }
            else { NSWorkspace.shared.open(url) }
        }
        return nil
    }
}
