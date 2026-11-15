#!/usr/bin/env bash
# ==============================================================================
# pfSense Captive Portal Auto-Login & macOS Menu Bar Setup
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo " pfSense Captive Portal Auto-Login & Top Bar Toggle Setup"
echo "=========================================================="

# 1. Check Python
PYTHON_BIN="$(which python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "[-] Python 3 not found. Please install Python 3 or Xcode Command Line Tools."
    exit 1
fi
echo "[+] Detected Python: $PYTHON_BIN ($($PYTHON_BIN --version))"

# 2. Virtual Environment Setup
if [ ! -d "venv" ]; then
    echo "[*] Creating Python virtual environment in venv/..."
    "$PYTHON_BIN" -m venv venv
fi

echo "[*] Installing/verifying Python dependencies..."
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install requests --quiet
echo "[+] Python dependencies installed."

# 3. Check / Create config.ini
if [ ! -f "config.ini" ]; then
    echo "[*] Creating config.ini from config.ini.example..."
    cp config.ini.example config.ini
    chmod 600 config.ini
    echo "[!] Please edit config.ini with your actual credentials before running."
else
    chmod 600 config.ini
    echo "[+] config.ini exists (permissions set to 600)."
fi

# 4. Build Menu Bar Top Bar App
echo "[*] Building native macOS Menu Bar application..."
if [ -f "menubar/build_menubar.sh" ]; then
    chmod +x menubar/build_menubar.sh
    ./menubar/build_menubar.sh
    echo "[+] Menu Bar app built at menubar/AutoLoginMenuBar.app"
else
    echo "[-] menubar/build_menubar.sh missing."
fi

# 5. Generate LaunchAgent plist
PLIST_NAME="com.pfsense.autologin.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/$PLIST_NAME"
mkdir -p "$HOME/Library/LaunchAgents"

sed -e "s|{{PYTHON_PATH}}|$SCRIPT_DIR/venv/bin/python|g" \
    -e "s|{{SCRIPT_PATH}}|$SCRIPT_DIR/portal_autologin.py|g" \
    -e "s|{{WORKING_DIR}}|$SCRIPT_DIR|g" \
    com.pfsense.autologin.plist.template > "$SCRIPT_DIR/$PLIST_NAME"

echo "[+] Generated LaunchAgent definition: $SCRIPT_DIR/$PLIST_NAME"

echo ""
echo "=========================================================="
echo " Installation Summary & Next Steps"
echo "=========================================================="
echo "1. Verify/Edit your portal credentials in config.ini:"
echo "   nano config.ini"
echo ""
echo "2. Install & Start LaunchAgent (Auto-evaluation every 60s):"
echo "   cp \"$SCRIPT_DIR/$PLIST_NAME\" \"$PLIST_TARGET\""
echo "   launchctl bootout gui/\$(id -u)/$PLIST_NAME 2>/dev/null || true"
echo "   launchctl bootstrap gui/\$(id -u) \"$PLIST_TARGET\""
echo ""
echo "3. Launch the Menu Bar Top Bar Toggle App:"
echo "   open \"$SCRIPT_DIR/menubar/AutoLoginMenuBar.app\""
echo "   (You will see the pfSense Wi-Fi icon in your top menu bar with an ON/OFF toggle!)"
echo "=========================================================="
