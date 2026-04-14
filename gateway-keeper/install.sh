#!/usr/bin/env bash
# gateway-keeper installer for Linux / macOS.
# Windows users: run install.ps1 from PowerShell.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "=== gateway-keeper installer ==="
echo "This will:"
echo "  1. Install Python deps (pyyaml)"
echo "  2. Copy example config if you don't have one"
echo "  3. Apply runtime patches to your OpenClaw install"
echo "  4. Optionally register a systemd/launchd service"
echo ""

# --- 1. python deps
if ! python3 -c "import yaml" 2>/dev/null; then
  echo "Installing PyYAML..."
  python3 -m pip install --user pyyaml >/dev/null
fi

# --- 2. config
if [[ ! -f gateway-keeper.yaml ]]; then
  cp gateway-keeper.example.yaml gateway-keeper.yaml
  echo "Created gateway-keeper.yaml from example. Edit it before starting if needed."
fi

# --- 3. apply patches (dry-run first so user sees what happens)
echo ""
echo "Dry-run patch check:"
python3 gateway_keeper.py apply-patches --dry-run || true

echo ""
read -r -p "Apply patches now? [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
  python3 gateway_keeper.py apply-patches
else
  echo "Skipping patch apply. You can run 'python3 gateway_keeper.py apply-patches' later."
fi

# --- 4. optional service registration
echo ""
read -r -p "Register as a system service? [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    echo "Writing launchd plist..."
    PLIST="$HOME/Library/LaunchAgents/com.jahfeelautomation.gateway-keeper.plist"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>    <string>com.jahfeelautomation.gateway-keeper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$HERE/gateway_keeper.py</string>
    <string>--config</string>
    <string>$HERE/gateway-keeper.yaml</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>WorkingDirectory</key> <string>$HERE</string>
  <key>StandardOutPath</key>  <string>$HOME/Library/Logs/gateway-keeper/stdout.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/gateway-keeper/stderr.log</string>
</dict>
</plist>
EOF
    mkdir -p "$HOME/Library/Logs/gateway-keeper"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Launched. Logs: ~/Library/Logs/gateway-keeper/"
  else
    echo "Writing systemd user unit..."
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/gateway-keeper.service" <<EOF
[Unit]
Description=gateway-keeper — OpenClaw reliability supervisor
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 $HERE/gateway_keeper.py --config $HERE/gateway-keeper.yaml start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now gateway-keeper.service
    echo "Started systemd user unit. View logs: journalctl --user -u gateway-keeper -f"
  fi
else
  echo "Run manually with: python3 $HERE/gateway_keeper.py start"
fi

echo ""
echo "=== Install complete ==="
