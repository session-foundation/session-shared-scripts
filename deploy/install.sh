#!/bin/sh
# Install or update session-ops on this host, from the clone this script sits in.
# Idempotent: run it again after every pull. As root.
#
# It creates the accounts, builds the venv, creates any missing env file empty, moves
# state and env files from the layout before session-ops@ units, installs the units
# and each job's drop-ins, and enables every job whose env files have content. It
# never edits nginx, which certbot owns; see deploy/README.md for the one route to add.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNITS=/etc/systemd/system
ETC=/etc/session-ops
STATE=/var/lib/session-ops
OPS="$ROOT/.venv/bin/session-ops"

[ "$(id -u)" = 0 ] || { echo "install.sh: run as root" >&2; exit 1; }
command -v uv >/dev/null || {
    echo "install.sh: uv is missing:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh" >&2
    exit 1
}

account() {
    id -u "$1" >/dev/null 2>&1 ||
        useradd --system --no-create-home --home /nonexistent --shell /usr/sbin/nologin "$1"
}
for user in ghdigest crowdin publisher sessionops; do account "$user"; done
# The Claude Code CLI keeps its binary, login and cache under this account's $HOME.
if ! id -u zendesk >/dev/null 2>&1; then
    useradd --system --home /home/zendesk --shell /usr/sbin/nologin zendesk
fi
install -d -o zendesk -g zendesk -m 700 /home/zendesk

# The system Python, not one uv downloads: that would live under root's home, which
# ProtectHome= hides from every unit.
UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --python /usr/bin/python3 \
    --directory "$ROOT" --quiet

# Moves a file from the previous layout, only when the new one does not exist yet.
move() {
    if [ -e "$1" ] && [ ! -e "$2" ]; then
        install -D -m "$3" -o "$4" -g "$4" "$1" "$2"
        echo "moved $1 -> $2 (the original is left in place)"
    fi
}
install -d -m 755 "$ETC"
move /etc/zendesk/env "$ETC/zendesk.env" 600 root
move /etc/github-prs/env "$ETC/github-prs.env" 600 root
move "$ETC/env" "$ETC/alerts.env" 600 root
# systemd reads EnvironmentFile= as root before dropping privileges, so these need
# no group: a job's account cannot read another job's secrets.
for name in zendesk github-prs crowdin publish alerts; do
    [ -e "$ETC/$name.env" ] || install -m 600 /dev/null "$ETC/$name.env"
done
# The publishing units load this as a credential whether or not a GitHub App is set
# up, and a missing file would stop them starting; empty means publish with a token.
[ -e "$ETC/github-app.pem" ] || install -m 600 /dev/null "$ETC/github-app.pem"

install -d -m 755 "$STATE"
install -d -o ghdigest -g ghdigest "$STATE/github-prs-digest"
install -d -o zendesk -g zendesk "$STATE/zendesk-digest"
move /var/lib/github-prs/seen.json "$STATE/github-prs-digest/seen.json" 640 ghdigest
move /var/lib/zendesk/seen.json "$STATE/zendesk-digest/seen.json" 640 zendesk
move /var/lib/zendesk/house_answers.json "$STATE/zendesk-digest/house_answers.json" 640 zendesk

install -m 644 "$ROOT/deploy/session-ops.tmpfiles" /etc/tmpfiles.d/session-ops.conf
systemd-tmpfiles --create session-ops.conf

# The units session-ops@ replaces.
for old in zendesk-digest github-prs-digest session-ops-silence crowdin-duplicates; do
    systemctl disable --now "$old.timer" 2>/dev/null || true
    rm -f "$UNITS/$old.service" "$UNITS/$old.timer"
done
rm -f "$UNITS/zendesk-alert@.service" "$UNITS/github-prs-alert@.service"

install -m 644 "$ROOT"/deploy/*.service "$ROOT"/deploy/*.timer "$UNITS/"
# Only the generated files go, so a drop-in added by hand survives.
rm -f "$UNITS"/session-ops@*.service.d/job.conf "$UNITS"/session-ops@*.timer.d/schedule.conf
"$OPS" units --out "$UNITS" >/dev/null
systemctl daemon-reload

for job in $("$OPS" list --ready); do
    systemctl enable --now "session-ops@$job.timer" >/dev/null
    echo "enabled session-ops@$job.timer"
done
for job in $("$OPS" list --not-ready); do
    echo "not enabled: session-ops@$job.timer (its env file is empty)"
done
for relay in zendesk-relay crowdin-relay; do
    env_file=$(systemctl show -p EnvironmentFiles --value "$relay.service" | cut -d' ' -f1)
    if [ -s "$env_file" ]; then
        systemctl enable "$relay.service" >/dev/null
        systemctl try-restart "$relay.service"
        systemctl start "$relay.service"
        echo "running $relay.service"
    else
        echo "not enabled: $relay.service ($env_file is empty)"
    fi
done
