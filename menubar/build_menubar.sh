#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/AutoLoginMenuBar.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"

echo "==> Compiling Swift Menu Bar app..."
swiftc -O "$SCRIPT_DIR/main.swift" -o "$SCRIPT_DIR/AutoLoginMenuBar"

echo "==> Packaging into AutoLoginMenuBar.app..."
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$SCRIPT_DIR/AutoLoginMenuBar" "$MACOS_DIR/AutoLoginMenuBar"
chmod +x "$MACOS_DIR/AutoLoginMenuBar"

cat << 'EOF' > "$CONTENTS_DIR/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>AutoLoginMenuBar</string>
    <key>CFBundleIdentifier</key>
    <string>com.pfsense.autologin.menubar</string>
    <key>CFBundleName</key>
    <string>AutoLoginMenuBar</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>2.0.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

echo "==> Build complete: $APP_DIR"
