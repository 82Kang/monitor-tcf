#!/bin/bash
# Stop and remove the launchd agent. Leaves state and logs alone.
set -euo pipefail
LABEL="com.bayantkang.monitor-tcf"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "removed: $LABEL"
