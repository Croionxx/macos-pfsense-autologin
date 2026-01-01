# pfSense Captive Portal Auto-Login for macOS

This project provides a **safe, reliable, and macOS-native auto-login solution** for networks that use **pfSense / Netgate captive portals** (hostels, campuses, dorms, etc.).

It automatically re-authenticates you when the captive portal session expires, without browser automation or insecure hacks.

---

## ✅ What This Solves

* Captive portal logs you out randomly
* Internet stops working until you re-login
* macOS does not reliably notify when this happens
* You want an automatic, safe fix

This tool:

* Detects loss of internet
* Detects captive portal availability
* Logs in automatically
* Exits immediately when internet is already working

---

## ✨ Features

* macOS-native (`launchd`)
* No root privileges
* No browser automation
* No credential leakage
* Works around macOS Wi-Fi SSID reporting bugs
* Safe retry logic with backoff
* Event-driven + watchdog fallback

---

## 📁 Repository Structure

```
wifi-autologin/
├── portal_autologin.py        # Main script
├── config.ini.example         # Template (no secrets)
├── README.md                  # This file
├── .gitignore                 # Prevents secret leaks
└── venv/                      # Python virtual environment (not committed)
```

---

## 🧠 How the Script Works (High Level)

1. **Triggered** by macOS network changes or periodic watchdog
2. Checks real internet connectivity (Google `generate_204`)
3. If internet is already up → exits immediately
4. If internet is down:

   * Checks captive portal availability
   * Sends login POST request
   * Verifies internet is restored
5. Exits cleanly (`--once` mode)

This ensures:

* No background loops
* No unnecessary requests
* No credential spam

---

## 🔍 Code Overview (`portal_autologin.py`)

### Key Components

| Function             | Purpose                          |
| -------------------- | -------------------------------- |
| `internet_up()`      | Detects real internet access     |
| `portal_available()` | Confirms captive portal presence |
| `login_pfsense()`    | Performs portal login            |
| `current_ssid()`     | Best-effort SSID detection       |
| `run(--once)`        | One-shot execution for stability |

### Important Design Decisions

* **SSID check is advisory** (macOS may report `None`)
* **Credentials are sent only if portal is confirmed**
* **Exponential backoff** prevents spamming
* **One-shot execution** works cleanly with `launchd`

---

## 🔧 Requirements

* macOS (Monterey / Ventura / Sonoma tested)
* Python 3.9+
* Network using pfSense / Netgate captive portal

---

## 🚀 Installation Guide

### 1️⃣ Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/wifi-autologin.git
cd wifi-autologin
```

---

### 2️⃣ Create a Python virtual environment (recommended)

```bash
/usr/bin/python3 -m venv venv
source venv/bin/activate
pip install requests
deactivate
```

Why:

* Works cleanly with macOS `launchd`
* No global installs
* No Nix / Homebrew issues

---

### 3️⃣ Configure credentials (IMPORTANT)

Copy the example file:

```bash
cp config.ini.example config.ini
```

Edit `config.ini`:

```ini
[credentials]
username = YOUR_USERNAME
password = YOUR_PASSWORD

[settings]
portal_url = http://172.20.28.1:8002/index.php?zone=hostelzone
connectivity_check_url = http://clients3.google.com/generate_204
retry_delay = 5
ssid = YOUR_WIFI_SSID
```

⚠️ **Never commit `config.ini` to GitHub.**

---

### 4️⃣ Test manually

```bash
venv/bin/python portal_autologin.py --once
```

Expected:

* If internet is already working → exits silently
* If captive portal is active → logs in and exits

---

## 🖥️ macOS Auto-Run Setup (launchd)

### 1️⃣ Create LaunchAgent

```bash
mkdir -p ~/Library/LaunchAgents
nano ~/Library/LaunchAgents/com.pfsense.autologin.plist
```

Paste the following:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>

  <key>Label</key>
  <string>com.pfsense.autologin</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USERNAME/path/to/wifi-autologin/venv/bin/python</string>
    <string>/Users/YOUR_USERNAME/path/to/wifi-autologin/portal_autologin.py</string>
    <string>--once</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/YOUR_USERNAME/path/to/wifi-autologin</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>NetworkState</key>
    <true/>
  </dict>

  <!-- Reliable fallback -->
  <key>StartInterval</key>
  <integer>60</integer>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/tmp/pfsense-autologin.out</string>
  <key>StandardErrorPath</key>
  <string>/tmp/pfsense-autologin.out</string>

</dict>
</plist>
```

---

### 2️⃣ Load the agent

```bash
launchctl unload ~/Library/LaunchAgents/com.pfsense.autologin.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.pfsense.autologin.plist
```

---

### 3️⃣ Verify logs

```bash
tail -f /tmp/pfsense-autologin.out
```

---

## 🔐 Security Considerations

* Credentials are sent **only** to the configured portal URL
* No wildcard endpoints
* No DNS guessing
* No browser automation
* No background credential leakage
* Script exits immediately if internet is already available

---

## ⚠️ Limitations

This will **not** work if the portal requires:

* JavaScript challenges
* CSRF tokens
* OTP / MFA
* TLS client certificates

---

## 🧪 Troubleshooting

Manual run:

```bash
venv/bin/python portal_autologin.py --once
```

Logs:

```bash
tail -f /tmp/pfsense-autologin.out
```

---

## 🛡️ GitHub Safety Checklist

* `config.ini` is ignored via `.gitignore`
* Only `config.ini.example` is committed
* `venv/` is ignored
* No credentials ever committed

---

## 📜 License

MIT License — use at your own risk.

---

## 🙌 Credits

Built for macOS users on pfSense / Netgate captive portal networks who want a **clean, reliable, non-hacky solution**.
