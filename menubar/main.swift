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

struct AppConfig {
    var username: String = ""
    var password: String = ""
    var portalUrl: String = "http://172.20.28.1:8002/index.php?zone=hostelzone"
    var connectivityCheckUrl: String = "http://clients3.google.com/generate_204"
    var ssid: String = ""
    var retryDelay: String = "5"
    var conflictCooldown: String = "900"

    static func load(from path: String) -> AppConfig {
        var config = AppConfig()
        guard let content = try? String(contentsOfFile: path, encoding: .utf8) else {
            return config
        }

        var currentSection = ""
        for line in content.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") || trimmed.hasPrefix(";") {
                continue
            }
            if trimmed.hasPrefix("[") && trimmed.hasSuffix("]") {
                currentSection = String(trimmed.dropFirst().dropLast()).lowercased()
                continue
            }
            let parts = trimmed.split(separator: "=", maxSplits: 1).map { String($0).trimmingCharacters(in: .whitespaces) }
            if parts.count == 2 {
                let key = parts[0].lowercased()
                let value = parts[1]
                if currentSection == "credentials" {
                    if key == "username" { config.username = value }
                    else if key == "password" { config.password = value }
                } else if currentSection == "settings" {
                    if key == "portal_url" { config.portalUrl = value }
                    else if key == "connectivity_check_url" { config.connectivityCheckUrl = value }
                    else if key == "ssid" { config.ssid = value }
                    else if key == "retry_delay" { config.retryDelay = value }
                    else if key == "conflict_cooldown" { config.conflictCooldown = value }
                }
            }
        }
        return config
    }

    func save(to path: String) -> Bool {
        let content = """
[credentials]
username = \(username)
password = \(password)

[settings]
portal_url = \(portalUrl)
connectivity_check_url = \(connectivityCheckUrl)
ssid = \(ssid)
retry_delay = \(retryDelay)
conflict_cooldown = \(conflictCooldown)
"""
        do {
            try content.write(toFile: path, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
            return true
        } catch {
            return false
        }
    }
}

// MARK: - Settings Window Controller
class SettingsWindowController: NSObject, NSWindowDelegate {
    static let shared = SettingsWindowController()

    var window: NSWindow?
    var usernameField: NSTextField!
    var passwordField: NSSecureTextField!
    var ssidField: NSTextField!
    var portalUrlField: NSTextField!
    var cooldownField: NSTextField!
    var statusLabel: NSTextField!

    var configPath: String {
        let appDelegate = NSApplication.shared.delegate as? AppDelegate
        let dir = appDelegate?.scriptDirectory ?? "/Users/mac/Documents/autologin_script"
        return "\(dir)/config.ini"
    }

    func showWindow() {
        if window == nil {
            createWindow()
        }

        loadConfigIntoFields()
        statusLabel.stringValue = ""

        guard let win = window else { return }
        NSApp.activate(ignoringOtherApps: true)
        win.center()
        win.makeKeyAndOrderFront(nil)
    }

    func createWindow() {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 420),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        win.title = "Auto-Login Settings"
        win.delegate = self
        win.isReleasedWhenClosed = false

        let contentView = NSView(frame: win.contentView!.bounds)
        contentView.autoresizingMask = [.width, .height]
        win.contentView = contentView

        var y: CGFloat = 370

        // Title
        let titleLabel = NSTextField(labelWithString: "Captive Portal Configuration")
        titleLabel.font = NSFont.boldSystemFont(ofSize: 15)
        titleLabel.frame = NSRect(x: 24, y: y, width: 432, height: 22)
        contentView.addSubview(titleLabel)

        y -= 20
        let subLabel = NSTextField(labelWithString: "Enter credentials and network settings:")
        subLabel.textColor = .secondaryLabelColor
        subLabel.font = NSFont.systemFont(ofSize: 12)
        subLabel.frame = NSRect(x: 24, y: y, width: 432, height: 18)
        contentView.addSubview(subLabel)

        func addFormRow(label: String, isSecure: Bool = false) -> NSTextField {
            y -= 48
            let lbl = NSTextField(labelWithString: label)
            lbl.font = NSFont.systemFont(ofSize: 12, weight: .medium)
            lbl.frame = NSRect(x: 24, y: y + 22, width: 432, height: 16)
            contentView.addSubview(lbl)

            if isSecure {
                let secField = NSSecureTextField(frame: NSRect(x: 24, y: y, width: 432, height: 22))
                contentView.addSubview(secField)
                return secField
            } else {
                let field = NSTextField(frame: NSRect(x: 24, y: y, width: 432, height: 22))
                contentView.addSubview(field)
                return field
            }
        }

        usernameField = addFormRow(label: "Username / Student ID:")
        passwordField = addFormRow(label: "Password:", isSecure: true) as? NSSecureTextField

        // SSID Row with Detect Button
        y -= 48
        let ssidLbl = NSTextField(labelWithString: "Target Wi-Fi SSID (leave blank for any network):")
        ssidLbl.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        ssidLbl.frame = NSRect(x: 24, y: y + 22, width: 432, height: 16)
        contentView.addSubview(ssidLbl)

        ssidField = NSTextField(frame: NSRect(x: 24, y: y, width: 310, height: 22))
        contentView.addSubview(ssidField)

        let detectBtn = NSButton(frame: NSRect(x: 342, y: y - 1, width: 114, height: 24))
        detectBtn.title = "Current Wi-Fi"
        detectBtn.bezelStyle = .rounded
        detectBtn.target = self
        detectBtn.action = #selector(detectCurrentWifi)
        contentView.addSubview(detectBtn)

        portalUrlField = addFormRow(label: "Portal Login URL:")
        cooldownField = addFormRow(label: "Phone Conflict Cooldown (seconds):")

        // Status Feedback Label
        y -= 28
        statusLabel = NSTextField(labelWithString: "")
        statusLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        statusLabel.frame = NSRect(x: 24, y: y, width: 432, height: 18)
        contentView.addSubview(statusLabel)

        // Buttons
        let saveButton = NSButton(frame: NSRect(x: 342, y: 16, width: 114, height: 32))
        saveButton.title = "Save"
        saveButton.bezelStyle = .rounded
        saveButton.keyEquivalent = "\r"
        saveButton.target = self
        saveButton.action = #selector(saveSettings)
        contentView.addSubview(saveButton)

        let testButton = NSButton(frame: NSRect(x: 220, y: 16, width: 114, height: 32))
        testButton.title = "Test Login"
        testButton.bezelStyle = .rounded
        testButton.target = self
        testButton.action = #selector(testLogin)
        contentView.addSubview(testButton)

        let cancelButton = NSButton(frame: NSRect(x: 100, y: 16, width: 114, height: 32))
        cancelButton.title = "Cancel"
        cancelButton.bezelStyle = .rounded
        cancelButton.target = self
        cancelButton.action = #selector(closeWindow)
        contentView.addSubview(cancelButton)

        self.window = win
    }

    func loadConfigIntoFields() {
        let config = AppConfig.load(from: configPath)
        usernameField.stringValue = config.username
        passwordField.stringValue = config.password
        ssidField.stringValue = config.ssid
        portalUrlField.stringValue = config.portalUrl
        cooldownField.stringValue = config.conflictCooldown
    }

    @objc func detectCurrentWifi() {
        guard let appDelegate = NSApplication.shared.delegate as? AppDelegate else { return }
        let state = appDelegate.readState()
        if let current = state.current_ssid, !current.isEmpty {
            ssidField.stringValue = current
            statusLabel.textColor = .labelColor
            statusLabel.stringValue = "Detected active network: \(current)"
        } else {
            statusLabel.textColor = .systemOrange
            statusLabel.stringValue = "No active Wi-Fi detected"
        }
    }

    @objc func saveSettings() {
        var config = AppConfig.load(from: configPath)
        config.username = usernameField.stringValue.trimmingCharacters(in: .whitespaces)
        config.password = passwordField.stringValue
        config.ssid = ssidField.stringValue.trimmingCharacters(in: .whitespaces)
        config.portalUrl = portalUrlField.stringValue.trimmingCharacters(in: .whitespaces)
        config.conflictCooldown = cooldownField.stringValue.trimmingCharacters(in: .whitespaces)

        if config.username.isEmpty || config.password.isEmpty {
            statusLabel.textColor = .systemRed
            statusLabel.stringValue = "Username and password cannot be empty."
            return
        }

        if config.save(to: configPath) {
            statusLabel.textColor = .systemGreen
            statusLabel.stringValue = "Settings saved successfully."
            if let appDelegate = NSApplication.shared.delegate as? AppDelegate {
                appDelegate.runScriptAsync(args: ["--force", "--once"])
            }
        } else {
            statusLabel.textColor = .systemRed
            statusLabel.stringValue = "Failed to save configuration."
        }
    }

    @objc func testLogin() {
        saveSettings()
        statusLabel.textColor = .labelColor
        statusLabel.stringValue = "Verifying authentication..."
        if let appDelegate = NSApplication.shared.delegate as? AppDelegate {
            appDelegate.runScriptAsync(args: ["--force", "--once"])
        }
    }

    @objc func closeWindow() {
        window?.close()
    }
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
        state.status_message = enabled ? "Enabled" : "Disabled"

        let dir = (stateFilePath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: nil)

        if let encoded = try? JSONEncoder().encode(state) {
            try? encoded.write(to: URL(fileURLWithPath: stateFilePath), options: .atomic)
        }

        let flag = enabled ? "--toggle-on" : "--toggle-off"
        runScriptAsync(args: [flag])
        refreshIconOnly()
    }

    // MARK: - Dedicated Captive Portal Icon (Key & Wireless Wave Symbol)
    // Avoids conflict with macOS native Wi-Fi 3-arc fan icon
    func refreshIconOnly() {
        let state = readState()
        let isEnabled = state.enabled ?? true
        let status = state.status ?? "INITIALIZING"
        updateIcon(status: status, enabled: isEnabled)
    }

    func updateIcon(status: String, enabled: Bool) {
        guard let button = statusItem.button else { return }

        let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .regular)

        if !enabled {
            let image = NSImage(systemSymbolName: "key.slash", accessibilityDescription: "Auto-Login Disabled")?.withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Auto-Login: Disabled"
            return
        }

        switch status {
        case "CONNECTED":
            // Key emitting wireless radio waves: distinct authentication symbol
            let image = NSImage(systemSymbolName: "key.radiowaves.forward", accessibilityDescription: "Auto-Login Connected")?.withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Auto-Login: Connected"

        case "AUTHENTICATING":
            let image = NSImage(systemSymbolName: "arrow.triangle.2.circlepath", accessibilityDescription: "Authenticating")?.withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Auto-Login: Authenticating..."

        case "CONFLICT_STANDBY", "STANDBY":
            let image = NSImage(systemSymbolName: "pause.circle", accessibilityDescription: "Standby")?.withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Auto-Login: Standby (Phone Active)"

        case "CAPTIVE":
            let image = NSImage(systemSymbolName: "exclamationmark.shield", accessibilityDescription: "Captive Portal")?.withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Auto-Login: Captive Portal"

        default:
            let image = NSImage(systemSymbolName: "key.radiowaves.forward", accessibilityDescription: "Auto-Login Active")?.withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Auto-Login: Active"
        }
    }

    // MARK: - Clean, Native HIG Menu Construction (Zero Emojis, Native Checkmarks)
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        let state = readState()
        let isEnabled = state.enabled ?? true
        let status = state.status ?? "INITIALIZING"
        let ssid = state.current_ssid ?? "Unknown"

        // 1. Status Section
        let statusDisplay = formatStatus(status)
        let statusItemMenu = NSMenuItem(title: "Status: \(statusDisplay)", action: nil, keyEquivalent: "")
        statusItemMenu.isEnabled = false
        menu.addItem(statusItemMenu)

        if let msg = state.status_message, !msg.isEmpty {
            let detailItem = NSMenuItem(title: msg, action: nil, keyEquivalent: "")
            detailItem.isEnabled = false
            menu.addItem(detailItem)
        }

        let networkItem = NSMenuItem(title: "Network: \(ssid)", action: nil, keyEquivalent: "")
        networkItem.isEnabled = false
        menu.addItem(networkItem)

        menu.addItem(NSMenuItem.separator())

        // 2. Native Checkmark Toggle Item (Uses macOS native .on / .off state)
        let toggleItem = NSMenuItem(title: "Enable Auto-Login", action: #selector(toggleEnabled), keyEquivalent: "")
        toggleItem.target = self
        toggleItem.state = isEnabled ? .on : .off
        menu.addItem(toggleItem)

        menu.addItem(NSMenuItem.separator())

        // 3. Settings & Credentials Dialog
        let settingsItem = NSMenuItem(title: "Settings...", action: #selector(openSettingsWindow), keyEquivalent: ",")
        settingsItem.target = self
        menu.addItem(settingsItem)

        menu.addItem(NSMenuItem.separator())

        // 4. Session Actions
        let forceItem = NSMenuItem(title: "Authenticate Now", action: #selector(forceLogin), keyEquivalent: "r")
        forceItem.target = self
        menu.addItem(forceItem)

        let logoutItem = NSMenuItem(title: "Release Session (Logout)", action: #selector(releaseSession), keyEquivalent: "l")
        logoutItem.target = self
        menu.addItem(logoutItem)

        if status == "CONFLICT_STANDBY" || status == "STANDBY" {
            let takeoverItem = NSMenuItem(title: "Take Over Session", action: #selector(takeoverSession), keyEquivalent: "k")
            takeoverItem.target = self
            menu.addItem(takeoverItem)
        }

        menu.addItem(NSMenuItem.separator())

        // 5. Utility
        let logItem = NSMenuItem(title: "View Log...", action: #selector(openLogFile), keyEquivalent: "")
        logItem.target = self
        menu.addItem(logItem)

        menu.addItem(NSMenuItem.separator())

        // 6. Quit
        let quitItem = NSMenuItem(title: "Quit Auto-Login", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
    }

    func formatStatus(_ raw: String) -> String {
        switch raw {
        case "CONNECTED": return "Connected"
        case "CAPTIVE": return "Captive Portal Detected"
        case "AUTHENTICATING": return "Authenticating"
        case "CONFLICT_STANDBY", "STANDBY": return "Standby (Phone Active)"
        case "AUTH_FAILED": return "Authentication Failed"
        case "QUOTA_EXHAUSTED", "QUOTA_EXCEEDED": return "Quota Reached"
        case "DEGRADED", "DISCONNECTED": return "Disconnected"
        case "DISABLED": return "Disabled"
        case "UNKNOWN_NETWORK": return "Other Network"
        default: return "Ready"
        }
    }

    // MARK: - Actions
    @objc func toggleEnabled() {
        let current = readState().enabled ?? true
        writeEnabledState(enabled: !current)
    }

    @objc func openSettingsWindow() {
        SettingsWindowController.shared.showWindow()
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
app.setActivationPolicy(.accessory)
app.run()
