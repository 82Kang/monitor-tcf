#!/bin/bash
# Install (or reinstall) the launchd agent that runs the watcher locally.
#
#   ./local/install.sh            # default: every 180 seconds
#   ./local/install.sh 300        # every 5 minutes
#
# Safe to re-run; it replaces any existing agent.

set -euo pipefail

INTERVAL="${1:-180}"
LABEL="com.bayantkang.monitor-tcf"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/.local/state/monitor-tcf"

mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR"
chmod +x "$REPO/local/run.sh"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$REPO/local/run.sh</string>
    </array>

    <!-- Run every $INTERVAL seconds. If the Mac was asleep, launchd fires once
         on wake rather than replaying every missed interval. -->
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>$STATE_DIR/launchd.out</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/launchd.err</string>
</dict>
</plist>
PLIST_EOF

# bootout/bootstrap are the modern commands; fall back for older macOS.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

echo "installed: $LABEL  (every ${INTERVAL}s)"
echo "plist:     $PLIST"
echo "state:     $STATE_DIR/state.json"
echo "log:       $STATE_DIR/watch.log"
