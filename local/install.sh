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

# Build one <dict><key>Minute</key>...</dict> per firing minute in the hour.
INTERVAL_MIN=$(( INTERVAL / 60 ))
[ "$INTERVAL_MIN" -lt 1 ] && INTERVAL_MIN=1
CALENDAR_ENTRIES=""
m=0
while [ "$m" -lt 60 ]; do
    CALENDAR_ENTRIES="$CALENDAR_ENTRIES        <dict><key>Minute</key><integer>$m</integer></dict>
"
    m=$(( m + INTERVAL_MIN ))
done

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

    <!-- Fire on wall-clock minutes rather than StartInterval.
         StartInterval got permanently deferred on this machine: after repeated
         sleep/wake cycles launchd parked it as "pended nondemand spawn =
         interval" and stopped firing entirely, for 22 hours, while the Mac was
         awake. Calendar triggers are evaluated against the clock instead and
         recover cleanly from sleep. -->
    <key>StartCalendarInterval</key>
    <array>
$CALENDAR_ENTRIES
    </array>

    <key>RunAtLoad</key>
    <true/>

    <!-- Deliberately NOT ProcessType=Background. That opts the job into power
         management deferral, which is what stalled it. A job that checks a
         webpage every few minutes costs nothing; being reliably run is the
         entire point. -->

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
