import SwiftUI
import WebKit

struct WebView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
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
