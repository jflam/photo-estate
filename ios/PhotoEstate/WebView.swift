import SwiftUI
import WebKit

struct WebView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        // A page shown inside the app (an Inbox item's) has the app's own tab bar under it: the web page's is hidden.
        config.userContentController.addUserScript(WKUserScript(
            source: "var s = document.createElement('style'); s.textContent = '.tabbar { display: none !important; }'; document.documentElement.appendChild(s);",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let view = WKWebView(frame: .zero, configuration: config)
        view.allowsBackForwardNavigationGestures = true
        view.scrollView.contentInsetAdjustmentBehavior = .never
        view.isOpaque = false
        view.backgroundColor = .clear
        let refresh = UIRefreshControl()
        refresh.addAction(UIAction { _ in view.reload(); refresh.endRefreshing() }, for: .valueChanged)
        view.scrollView.refreshControl = refresh
        if let c = Server.demoCookie {        // (a debug build as a demo member: Server.swift)
            config.websiteDataStore.httpCookieStore.setCookie(c) { view.load(URLRequest(url: url)) }
        } else {
            view.load(URLRequest(url: url))
        }
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}
}
