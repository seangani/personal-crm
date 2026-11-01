#!/bin/bash
# Keep the CRM server running in the background (starts at login, restarts if it stops)
# and connect it to Claude Code. Run once:  bash install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.seangani.crm.plist"
mkdir -p "$DIR/data"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.seangani.crm</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>-m</string><string>crm.app</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
</dict></plist>
PL
pkill -f "crm.app" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.seangani.crm" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for i in $(seq 30); do curl -sf -o /dev/null http://localhost:8787/ && break; sleep 1; done
curl -sf -o /dev/null http://localhost:8787/ && echo "CRM running at http://localhost:8787" || echo "CRM didn't start, see $DIR/data/server.log"
claude mcp get crm >/dev/null 2>&1 || claude mcp add --transport http --scope user crm http://localhost:8787/mcp
echo "Connected to Claude Code as 'crm'. Open http://localhost:8787 in Safari, then File > Add to Dock."
