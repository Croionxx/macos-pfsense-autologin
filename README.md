# pfSense Captive Portal Auto-Login with macOS Menu Bar Toggle

A reliable, native macOS auto-login solution and menu bar companion for networks protected by **pfSense / Netgate captive portals** (hostels, university dorms, colleges, campuses, and enterprise guest zones).

It automatically authenticates when your captive session expires, handles **concurrent session limits** without fighting other devices (e.g., your smartphone), extracts **CSRF tokens and dynamic form actions**, and gives you a **top bar toggle button** in macOS to turn auto-login ON or OFF instantly.

---

## 🚀 Key Improvements & Features

### 1. 🛡️ Anti-Flapping Session Arbitration (Solving Phone vs. Mac Conflicts)
Many campus and hostel networks enforce **strict single-session limits** (`Concurrent User Logins: Disabled`). When your smartphone connects, the captive portal drops your Mac. A naive script would immediately re-authenticate, kicking your phone off, causing your phone to re-login, creating an infinite **ping-pong disconnection storm** that gets your account or MAC blacklisted!
* **Response Body Inspection**: Detects portal error messages (*"maximum session limit reached"*, *"already logged in from another IP"*, *"simultaneous use"*).
* **Smart Standby Mode**: When another device is active, the engine enters a cooldown standby (default 15 minutes) instead of spamming login requests.
* **Release Session (Logout)**: Supports explicit logout (`--logout` or via Menu Bar) so you can release the captive slot for your phone without having to disable your Wi-Fi!
* **Take Over Session**: Allows you to override standby with one click whenever you need your Mac to take back the connection.

### 2. 🎛️ macOS Menu Bar Toggle (Top Bar Button)
A native macOS top bar status item (`AutoLoginMenuBar.app`) built with Swift and AppKit (`LSUIElement`):
* **Live Status Icon**:
  - 🟢 **Connected**: Internet access confirmed.
  - 🟡 **Captive**: Behind captive portal, preparing login.
  - 🔵 **Authenticating**: Login in progress.
  - 🟠 **Standby**: Session active on phone/another device; flapping prevented.
  - ⚪ **Disabled**: Auto-login turned off by user.
* **Instant Toggle**: Click `✓ Auto-Login: Enabled` to turn auto-login ON or OFF with zero delay.
* **Graphical Settings & Credentials Manager**:
  - Click `⚙️ Settings & Credentials...` directly from the top menu bar to open a native preferences window.
  - Enter and save your **Username**, **Password**, **Target Wi-Fi SSID** (with an automatic "Current Wi-Fi" detection button), **Portal URL**, and **Standby Cooldown** without ever opening a terminal, text editor, or editing backend code!
  - Credentials are automatically saved with hardened file permissions (`chmod 600`).
* **Quick Actions**:
  - `⚡ Authenticate Now (Force)`: Forces immediate authentication.
  - `🚪 Release Session (Logout)`: Drops the session on pfSense so your phone can use it.
  - `📲 Take Over Session From Phone`: Clears standby and claims the connection.
  - `📄 View Log File...`: Opens the live activity log.

### 3. 🔒 Single-Instance Process Mutex (`flock`)
* Uses non-blocking OS file locks (`fcntl.flock`).
* If `launchd` triggers while a previous check is running or if you manually trigger a check, duplicate instances cleanly exit, preventing socket exhaustion and credential spam.

### 4. 🧩 Dynamic Form & CSRF Token Extraction
* Modern pfSense setups enforce CSRF security tokens (`__csrf_magic` and zone nonces) or redirect to dynamic HTTPS ports (e.g. port `8003`).
* Built-in HTML form parser automatically inspects the landing page, extracts hidden tokens and dynamic form actions, and submits them along with your credentials.

### 5. ⚡ Modern macOS `launchd` Supervisor
* Completely removed the deprecated `KeepAlive: NetworkState` directive that caused CPU-heavy 10-second respawn storms on modern macOS (Ventura, Sonoma, Sequoia).
* Runs on a clean 60-second watchdog interval using modern `launchctl bootstrap` / `bootout` commands.
* If disabled via the Menu Bar toggle, each background check exits in `< 5ms` with zero network overhead.

---

## 📁 Repository Structure

```
autologin_script/
├── portal_autologin.py           # Core engine & session arbitration
├── config.ini.example            # Configuration template (no secrets)
├── config.ini                    # Local configuration (chmod 600, gitignored)
├── com.pfsense.autologin.plist   # LaunchAgent service definition
├── setup.sh                      # One-click installation & build script
├── menubar/
│   ├── main.swift                # Native Swift Menu Bar Top Bar app
│   ├── build_menubar.sh          # Swift compilation & app packaging script
│   └── AutoLoginMenuBar.app      # Standalone macOS Menu Bar application
└── tests/
    └── test_autologin.py         # Comprehensive unit test suite (>=85% coverage)
```

---

## 📦 Quick Installation

Run the automated setup script:

```bash
chmod +x setup.sh
./setup.sh
```

This will:
1. Initialize a Python virtual environment (`venv/`).
2. Install `requests`.
3. Create `config.ini` from `config.ini.example` (with `chmod 600` permissions).
4. Compile the native Swift Menu Bar app (`AutoLoginMenuBar.app`).
5. Generate the configured `com.pfsense.autologin.plist` LaunchAgent for your user.

---

## ⚙️ Configuration (`config.ini`)

Edit `config.ini` with your credentials and portal URL:

```ini
[credentials]
username = YOUR_PORTAL_USERNAME
password = YOUR_PORTAL_PASSWORD

[settings]
# Captive portal login URL
portal_url = http://172.20.28.1:8002/index.php?zone=hostelzone

# Connectivity check endpoint (returns HTTP 204 when internet is UP)
connectivity_check_url = http://clients3.google.com/generate_204

# Target Wi-Fi SSID (leave empty to allow any network)
ssid = MBH2_WiFi

# Delay in seconds between connection retries
retry_delay = 5

# Standby cooldown in seconds when another device is detected (default 900s = 15 min)
conflict_cooldown = 900
```

---

## 🖥️ Running the Menu Bar App

Simply launch the built application:

```bash
open menubar/AutoLoginMenuBar.app
```

You will see the Wi-Fi icon appear in your macOS top menu bar. Click it to view the live connection status, toggle auto-login on/off, or release/force sessions!

*Tip: To have the Menu Bar app launch automatically on Mac login, go to **System Settings > General > Login Items** and add `AutoLoginMenuBar.app` to "Open at Login".*

---

## ⏰ Enabling Background Automation (`launchd`)

To have macOS automatically check and reconnect in the background:

```bash
# 1. Copy the plist to your LaunchAgents directory
cp com.pfsense.autologin.plist ~/Library/LaunchAgents/

# 2. Load the service using modern launchctl syntax
launchctl bootout gui/$(id -u)/com.pfsense.autologin 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pfsense.autologin.plist
```

To stop or unload:
```bash
launchctl bootout gui/$(id -u)/com.pfsense.autologin
```

---

## 🧪 Testing & Verification

Run the comprehensive unit test suite:

```bash
venv/bin/python -m unittest tests/test_autologin.py
```

Check test coverage:
```bash
venv/bin/coverage run -m unittest tests/test_autologin.py
venv/bin/coverage report -m
```

---

## 🛠️ CLI Commands & Manual Usage

```bash
# Evaluate once and exit
venv/bin/python portal_autologin.py --once

# Force login (bypassing any active phone standby)
venv/bin/python portal_autologin.py --force --once

# Release captive session (logout) so phone can connect
venv/bin/python portal_autologin.py --logout

# Toggle auto-login on or off from the terminal
venv/bin/python portal_autologin.py --toggle-on
venv/bin/python portal_autologin.py --toggle-off

# Print current state JSON
venv/bin/python portal_autologin.py --status-json

# Run as standalone continuous daemon (without launchd)
venv/bin/python portal_autologin.py --daemon
```

---

## 📜 License
MIT License.
