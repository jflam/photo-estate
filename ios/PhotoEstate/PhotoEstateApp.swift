import SwiftUI

@main
struct PhotoEstateApp: App {
    init() { Server.useDemoMember() }       // a debug build pointed at a demo server, as one of its members (Server.swift)

    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
