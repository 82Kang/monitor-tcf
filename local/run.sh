#!/bin/bash
# Local runner for the TCF watcher. launchd invokes this every few minutes.
#
# Deliberately different from the GitHub Actions path in two ways:
#   - state lives outside the repo, so local runs never fight git
#   - credentials come from a chmod 600 env file, never the repo
#
# Always exits 0: launchd throttles jobs that keep failing, and the real exit
# code is recorded in the log instead.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.local/state/monitor-tcf"
ENV_FILE="$HOME/.config/monitor-tcf/env"
LOG="$STATE_DIR/watch.log"

mkdir -p "$STATE_DIR"

# launchd gives a job almost no environment, so never rely on PATH.
PYTHON=""
for candidate in /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
    [ -x "$candidate" ] && { PYTHON="$candidate"; break; }
done
if [ -z "$PYTHON" ]; then
    printf '%s | FATAL no python3 found\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" >> "$LOG"
    exit 0
fi

# Credentials, if they have been set up yet. Without them check.py still runs
# and reports what it found - it just cannot send.
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Identify this runner in every message, and give it its own heartbeat hour so
# the Mac and the cloud job never shout at the same minute.
export WATCHER_LABEL="${WATCHER_LABEL:-TCF watcher (Mac)}"
export HEARTBEAT_HOUR_UTC="${HEARTBEAT_HOUR_UTC:-13}"

output=$("$PYTHON" "$REPO/check.py" --state "$STATE_DIR/state.json" 2>&1)
code=$?

printf '%s | exit=%s | %s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S%z')" \
    "$code" \
    "$(printf '%s' "$output" | tr '\n' '~' | sed 's/~/ | /g')" >> "$LOG"

# Rotate so an unattended watcher cannot fill the disk over months.
if [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
    tail -n 3000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit 0
