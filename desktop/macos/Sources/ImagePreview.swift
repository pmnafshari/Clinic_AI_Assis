import AppKit
import WebKit

/// A clinic image (a guide page, a document image) opened on its own: a preview window with the environment's own
/// web data store, so it loads under the same sign-in and the clinic's role checks; it cannot navigate anywhere.
/// The main page stays where it was - an image shown in its place is an opaque, sandboxed document, and the next
/// navigation started from it went out without the staff session (it showed the sign-in page; found in P28 M3).
final class ImagePreview: NSObject, WKNavigationDelegate {
    static var open: [ImagePreview] = []
    let window: NSWindow
    let url: URL
    private let web: WKWebView

    init(url: URL, store: WKWebsiteDataStore) {
        self.url = url
        let config = WKWebViewConfiguration()
        config.websiteDataStore = store
        web = WKWebView(frame: .zero, configuration: config)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 900),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        window.title = "Preview - \(url.lastPathComponent == "image" ? url.deletingLastPathComponent().lastPathComponent : url.lastPathComponent)"
        window.isReleasedWhenClosed = false
        window.contentView = web
        web.navigationDelegate = self
        web.setAccessibilityLabel("Image preview")
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { [weak self] _ in
            ImagePreview.open.removeAll { $0 === self }
        }
        Self.open.append(self)
        web.load(URLRequest(url: url))
        window.center()
        window.makeKeyAndOrderFront(nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(action.request.url == url ? .allow : .cancel)
    }
}
