import AppKit
import Foundation

// MARK: - Models and State
struct AutoLoginState: Codable {
    var enabled: Bool?
    var status: String?
    var status_message: String?
    var last_checked: Double?
    var standby_until: Double?
    var standby_reason: String?
    var current_ssid: String?
    var portal_url: String?
}

// MARK: - App Delegate
class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    var menu: NSMenu!
    var timer: Timer?

    let stateFilePath: String = {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return "\(home)/.config/pfsense-autologin/state.json"
    }()

    let scriptDirectory: String = {
        let bundlePath = Bundle.main.bundlePath
        let bundleDir = (bundlePath as NSString).deletingLastPathComponent
        if FileManager.default.fileExists(atPath: "\(bundleDir)/portal_autologin.py") {
            return bundleDir
        }
        let parentDir = (bundleDir as NSString).deletingLastPathComponent
        if FileManager.default.fileExists(atPath: "\(parentDir)/portal_autologin.py") {
            return parentDir
        }
        let defaultPath = "/Users/mac/Documents/autologin_script"
        if FileManager.default.fileExists(atPath: "\(defaultPath)/portal_autologin.py") {
            return defaultPath
        }
        return bundleDir
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        refreshIconOnly()

        // Timer to periodically poll state and update the menu bar icon
        // Does NOT rebuild the menu while the user is actively viewing it
        timer = Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            self?.refreshIconOnly()
        }
    }

    // MARK: - State Management
    func readState() -> AutoLoginState {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: stateFilePath)),
              let state = try? JSONDecoder().decode(AutoLoginState.self, from: data) else {
            return AutoLoginState(enabled: true, status: "READY", status_message: "Auto-login active", last_checked: 0, standby_until: 0, standby_reason: nil, current_ssid: nil, portal_url: nil)
        }
        return state
    }

    func writeEnabledState(enabled: Bool) {
        var state = readState()
        state.enabled = enabled
        state.status = enabled ? "INITIALIZING" : "DISABLED"
        state.status_message = enabled ? "Enabled via Menu Bar" : "Disabled via Menu Bar"

        let dir = (stateFilePath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: nil)

        if let encoded = try? JSONEncoder().encode(state) {
            // Atomic file write to avoid partial reads
            try? encoded.write(to: URL(fileURLWithPath: stateFilePath), options: .atomic)
        }

        // Notify CLI worker
        let flag = enabled ? "--toggle-on" : "--toggle-off"
        runScriptAsync(args: [flag])
        refreshIconOnly()
    }

    // MARK: - Icon Updating (Lightweight, Non-Disruptive)
    func refreshIconOnly() {
        let state = readState()
        let isEnabled = state.enabled ?? true
        let status = state.status ?? "INITIALIZING"
        updateIcon(status: status, enabled: isEnabled)
    }

    func updateIcon(status: String, enabled: Bool) {
        guard let button = statusItem.button else { return }

        if !enabled {
            if #available(macOS 11.0, *) {
                let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .regular)
                let image = NSImage(systemSymbolName: "wifi.slash", accessibilityDescription: "Disabled")?.withSymbolConfiguration(config)
                image?.isTemplate = true
                button.image = image
            } else {
                button.title = "⚪ pfSense"
            }
            button.toolTip = "pfSense Auto-Login: Disabled"
            return
        }

        switch status {
        case "CONNECTED":
            if #available(macOS 11.0, *) {
                let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
                let image = NSImage(systemSymbolName: "wifi", accessibilityDescription: "Connected")?.withSymbolConfiguration(config)
                image?.isTemplate = true
                button.image = image
            } else {
                button.title = "🟢 pfSense"
            }
            button.toolTip = "pfSense: Internet Connected"

        case "AUTHENTICATING":
            if #available(macOS 11.0, *) {
                let image = NSImage(systemSymbolName: "arrow.triangle.2.circlepath", accessibilityDescription: "Logging in")
                image?.isTemplate = true
                button.image = image
            } else {
                button.title = "🔵 pfSense"
            }
            button.toolTip = "pfSense: Authenticating..."

        case "CONFLICT_STANDBY":
            if #available(macOS 11.0, *) {
                let image = NSImage(systemSymbolName: "person.2.slash", accessibilityDescription: "Session Conflict")
                image?.isTemplate = true
                button.image = image
            } else {
                button.title = "🟠 Standby"
            }
            button.toolTip = "pfSense: Standby (Phone Active)"

        case "CAPTIVE":
            if #available(macOS 11.0, *) {
                let image = NSImage(systemSymbolName: "wifi.exclamationmark", accessibilityDescription: "Captive Portal")
                image?.isTemplate = true
                button.image = image
            } else {
                button.title = "🟡 Captive"
            }
            button.toolTip = "pfSense: Captive Portal Detected"

        default:
            if #available(macOS 11.0, *) {
                let image = NSImage(systemSymbolName: "wifi", accessibilityDescription: "Active")
                image?.isTemplate = true
                button.image = image
            } else {
                button.title = "pfSense"
            }
            button.toolTip = "pfSense Auto-Login"
        }
    }

    // MARK: - NSMenuDelegate (Rebuilds only on user click)
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        let state = readState()
        let isEnabled = state.enabled ?? true
        let status = state.status ?? "INITIALIZING"
        let ssid = state.current_ssid ?? "Unknown"

        // 1. Status Header
        let statusTitle = "● Status: \(status)"
        let statusItemMenu = NSMenuItem(title: statusTitle, action: nil, keyEquivalent: "")
        statusItemMenu.isEnabled = false
        menu.addItem(statusItemMenu)

        if let msg = state.status_message, !msg.isEmpty {
            let detailItem = NSMenuItem(title: "   \(msg)", action: nil, keyEquivalent: "")
            detailItem.isEnabled = false
            menu.addItem(detailItem)
        }

        let networkItem = NSMenuItem(title: "   Network: \(ssid)", action: nil, keyEquivalent: "")
        networkItem.isEnabled = false
        menu.addItem(networkItem)

        menu.addItem(NSMenuItem.separator())

        // 2. Main ON/OFF Toggle Item
        let toggleTitle = isEnabled ? "✓ Auto-Login: Enabled" : "   Auto-Login: Disabled (Click to Enable)"
        let toggleItem = NSMenuItem(title: toggleTitle, action: #selector(toggleEnabled), keyEquivalent: "t")
        toggleItem.target = self
        menu.addItem(toggleItem)

        menu.addItem(NSMenuItem.separator())

        // 3. Session Controls
        let forceItem = NSMenuItem(title: "⚡ Authenticate Now (Force)", action: #selector(forceLogin), keyEquivalent: "r")
        forceItem.target = self
        menu.addItem(forceItem)

        let logoutItem = NSMenuItem(title: "🚪 Release Session (Logout for Phone)", action: #selector(releaseSession), keyEquivalent: "l")
        logoutItem.target = self
        menu.addItem(logoutItem)

        if status == "CONFLICT_STANDBY" {
            let takeoverItem = NSMenuItem(title: "📲 Take Over Session From Phone", action: #selector(takeoverSession), keyEquivalent: "k")
            takeoverItem.target = self
            menu.addItem(takeoverItem)
        }

        menu.addItem(NSMenuItem.separator())

        // 4. Utility Actions
        let logItem = NSMenuItem(title: "📄 View Log File...", action: #selector(openLogFile), keyEquivalent: "")
        logItem.target = self
        menu.addItem(logItem)

        let configItem = NSMenuItem(title: "⚙️ Edit Config (config.ini)...", action: #selector(openConfigFile), keyEquivalent: "")
        configItem.target = self
        menu.addItem(configItem)

        menu.addItem(NSMenuItem.separator())

        // 5. Quit
        let quitItem = NSMenuItem(title: "Quit Menu Bar App", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
    }

    // MARK: - Actions
    @objc func toggleEnabled() {
        let current = readState().enabled ?? true
        writeEnabledState(enabled: !current)
    }

    @objc func forceLogin() {
        runScriptAsync(args: ["--force", "--once"])
    }

    @objc func releaseSession() {
        runScriptAsync(args: ["--logout"])
    }

    @objc func takeoverSession() {
        runScriptAsync(args: ["--force", "--once"])
    }

    @objc func openLogFile() {
        let logPath = "\(scriptDirectory)/autologin.log"
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil, attributes: nil)
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: logPath))
    }

    @objc func openConfigFile() {
        let cfgPath = "\(scriptDirectory)/config.ini"
        NSWorkspace.shared.open(URL(fileURLWithPath: cfgPath))
    }

    @objc func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    // MARK: - Subprocess Execution
    func runScriptAsync(args: [String]) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let venvPython = "\(self.scriptDirectory)/venv/bin/python"
            let pythonBin = FileManager.default.fileExists(atPath: venvPython) ? venvPython : "/usr/bin/python3"
            let scriptPath = "\(self.scriptDirectory)/portal_autologin.py"

            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: pythonBin)
            proc.arguments = [scriptPath] + args
            proc.currentDirectoryURL = URL(fileURLWithPath: self.scriptDirectory)

            try? proc.run()
            proc.waitUntilExit()

            DispatchQueue.main.async {
                self.refreshIconOnly()
            }
        }
    }
}

// MARK: - Main Entry Point
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory) // Accessory prevents icon in Dock
app.run()
