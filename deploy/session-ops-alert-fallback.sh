#!/bin/sh
# Report that session-ops-alert@ itself failed, from session-ops-alert-fallback@.service.
#
# Shell and curl only: the alert runs from the same venv as every job, so a venv that
# stops importing (a Python upgrade under it) fails the jobs and silences their alerts.
#
# Usage: session-ops-alert-fallback.sh <failed unit>
set -u

unit=${1:?usage: session-ops-alert-fallback.sh <unit>}
# One post per window: a broken venv fails every run of every job, each a new alert.
WINDOW_SECONDS=${WINDOW_SECONDS:-21600}
marker="${STATE_DIRECTORY:?}/last-post"

if [ -z "${ALERT_DISCORD_WEBHOOK_URL:-}" ]; then
    echo "No ALERT_DISCORD_WEBHOOK_URL in alerts.env, so $unit is not reported."
    exit 0
fi

now=$(date +%s)
last=$(cat "$marker" 2>/dev/null || echo 0)
case $last in '' | *[!0-9]*) last=0 ;; esac
if [ $((now - last)) -lt "$WINDOW_SECONDS" ]; then
    echo "Posted at $last, under ${WINDOW_SECONDS}s ago, so $unit is not reported again."
    exit 0
fi

# JSON string content: backslashes and quotes escaped, control characters dropped.
json() {
    printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

line=$(journalctl "_SYSTEMD_UNIT=$unit" -n 1 -q -o cat --no-pager 2>/dev/null | cut -c1-400)
host=$(hostname)
text="🚨 **$(json "$unit")** failed on \`$(json "$host")\`, so failures of session-ops units there are not being reported."
[ -n "$line" ] && text="$text\\n> $(json "$line")"
text="$text\\nA ModuleNotFoundError means the venv no longer imports, usually after a Python upgrade: run \`/opt/session-ops/deploy/install.sh\` as root."
text="$text\\n\`journalctl -u $(json "$unit") -n 50 --no-pager\`"

payload="{\"content\": \"$text\", \"allowed_mentions\": {\"parse\": []}}"
if ! printf '%s' "$payload" | curl -fsS --max-time 30 -H 'Content-Type: application/json' \
    --data-binary @- "$ALERT_DISCORD_WEBHOOK_URL" >/dev/null; then
    echo "Could not post the failure of $unit to Discord." >&2
    exit 1
fi
printf '%s\n' "$now" >"$marker"
