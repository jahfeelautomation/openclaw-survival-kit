#!/usr/bin/env bash
# claw-reaper installer for Linux / macOS.
# Windows users: run install.ps1 from PowerShell.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "=== claw-reaper installer ==="
echo "This will:"
echo "  1. Install Python deps (pyyaml)"
echo "  2. Copy example config if you don't have one"
echo "  3. Run a one-shot config check against your openclaw.json"
echo "  4. Optionally register a systemd/launchd service (started STOPPED)"
echo ""

# --- 1. python deps
if ! python3 -c "import yaml" 2>/dev/null; then
  echo "Installing PyYAML..."
  python3 -m pip install --user pyyaml >/dev/null
fi

# --- 2. config
if [[ ! -f claw-reaper.yaml ]]; then
  cp claw-reaper.example.yaml claw-reaper.yaml
  echo "Created claw-reaper.yaml from example. Edit it before starting if needed."
fi

# --- 3. pre-flight config check (non-destructive)
echo ""
echo "Running pre-flight config check..."
python3 claw_reaper.py --config "$HERE/claw-reaper.yaml" check || true

echo ""
read -r -p "Proceed with service registration? (leaves service STOPPED) [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    echo "Writing launchd plist..."
    PLIST="$HOME/Library/LaunchAgents/com.jahfeelautomation.claw-reaper.plist"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>    <string>com.jahfeelautomation.claw-reaper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$HERE/claw_reaper.py</string>
    <string>--config</string>
    <string>$HERE/claw-reaper.yaml</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>        <false/>
  <key>KeepAlive</key>        <true/>
  <key>WorkingDirectory</key> <string>$HERE</string>
  <key>StandardOutPath</key>  <string>$HOME/Library/Logs/claw-reaper/stdout.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/claw-reaper/stderr.log</string>
</dict>
</plist>
EOF
    mkdir -p "$HOME/Library/Logs/claw-reaper"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Registered (stopped). Start with: launchctl start com.jahfeelautomation.claw-reaper"
    echo "Logs: ~/Library/Logs/claw-reaper/"
  else
    echo "Writing systemd user unit..."
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/claw-reaper.service" <<EOF
[Unit]
Description=claw-reaper — OpenClaw RAM-bomb guard + checkpoint reaper
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 $HERE/claw_reaper.py --config $HERE/claw-reaper.yaml start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    # Enable but do NOT start automatically — operator reviews the check
    # output first and opts in.
    systemctl --user enable claw-reaper.service
    echo "Registered (stopped). Start with: systemctl --user start claw-reaper"
    echo "View logs: journalctl --user -u claw-reaper -f"
  fi
else
  echo "Run manually with: python3 $HERE/claw_reaper.py start"
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "Next: review the pre-flight report above. If you saw WARN lines,"
echo "fix your openclaw.json before starting the service."
