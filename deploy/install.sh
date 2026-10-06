#!/bin/sh
# Install or update session-ops on this host, from the clone this script sits in.
# Idempotent: run it again after every pull. As root.
#
# It creates the accounts, builds the venv, creates any missing env file empty, moves
# state and env files from the layout before session-ops@ units, installs the units
# and each job's drop-ins, and enables every job whose env files have content and no
# other: on its own timer, or on the queue's. It never edits nginx, which certbot owns; see deploy/README.md for the route.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNITS=/etc/systemd/system
ETC=/etc/session-ops
STATE=/var/lib/session-ops
OPS="$ROOT/.venv/bin/session-ops"

# Every unit runs /opt/session-ops/.venv, so installed from any other clone they all
# fail to start; and they run its code, so only root may be able to change it.
[ "$ROOT" = /opt/session-ops ] || {
    echo "install.sh: run the clone at /opt/session-ops, not $ROOT" >&2
    exit 1
}
[ "$(id -u)" = 0 ] || { echo "install.sh: run as root" >&2; exit 1; }
unsafe=$(find "$ROOT" -xdev \( ! -user root -o \( ! -type l -perm /022 \) \) -print -quit)
[ -z "$unsafe" ] || {
    echo "install.sh: $unsafe is writable by someone other than root:" >&2
    echo "  chown -R root:root $ROOT && chmod -R go-w $ROOT" >&2
    exit 1
}
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

# Stopped before their state is copied, so none of them writes it after the copy.
for old in zendesk-digest github-prs-digest session-ops-silence crowdin-duplicates; do
    systemctl disable --now "$old.timer" 2>/dev/null || true
    systemctl stop "$old.service" 2>/dev/null || true
done

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
# What each file takes, commented; the env files stay empty until filled, since a job
# is enabled once its env files have content.
install -m 644 "$ROOT"/deploy/env/*.example "$ETC/"
# The publishing units load this as a credential, and a missing file would stop them
# starting; left empty, their runs exit naming the key.
[ -e "$ETC/github-app.pem" ] || install -m 600 /dev/null "$ETC/github-app.pem"

install -d -m 755 "$STATE"
install -d -o ghdigest -g ghdigest "$STATE/github-prs-digest"
install -d -o zendesk -g zendesk "$STATE/zendesk-digest"
move /var/lib/github-prs/seen.json "$STATE/github-prs-digest/seen.json" 640 ghdigest
move /var/lib/zendesk/seen.json "$STATE/zendesk-digest/seen.json" 640 zendesk
move /var/lib/zendesk/house_answers.json "$STATE/zendesk-digest/house_answers.json" 640 zendesk
# zendesk.env was copied as is, still naming the old path, which the README says to remove.
OLD_HOUSE=/var/lib/zendesk/house_answers.json
NEW_HOUSE="$STATE/zendesk-digest/house_answers.json"
if [ -e "$NEW_HOUSE" ] && grep -qx "ZENDESK_HOUSE_ANSWERS=$OLD_HOUSE" "$ETC/zendesk.env"; then
    sed -i "s|^ZENDESK_HOUSE_ANSWERS=$OLD_HOUSE\$|ZENDESK_HOUSE_ANSWERS=$NEW_HOUSE|" "$ETC/zendesk.env"
    echo "pointed ZENDESK_HOUSE_ANSWERS in $ETC/zendesk.env at $NEW_HOUSE"
fi

install -m 644 "$ROOT/deploy/session-ops.tmpfiles" /etc/tmpfiles.d/session-ops.conf
systemd-tmpfiles --create session-ops.conf

# The units session-ops@ replaces.
for old in zendesk-digest github-prs-digest session-ops-silence crowdin-duplicates; do
    rm -f "$UNITS/$old.service" "$UNITS/$old.timer"
done
rm -f "$UNITS/zendesk-alert@.service" "$UNITS/github-prs-alert@.service"
# Retired: the daily reconciliation alone keeps the duplicates state.
systemctl disable --now crowdin-relay.service 2>/dev/null || true
rm -f "$UNITS/crowdin-relay.service"

install -m 644 "$ROOT"/deploy/*.service "$ROOT"/deploy/*.timer "$UNITS/"
# Only the generated files go, so a drop-in added by hand survives.
rm -f "$UNITS"/session-ops@*.service.d/job.conf "$UNITS"/session-ops@*.timer.d/schedule.conf \
    "$UNITS"/session-ops-queue.timer.d/schedule.conf
"$OPS" units --out "$UNITS" >/dev/null

READY=$("$OPS" list --ready)
QUEUED=$("$OPS" list --queued)
listed() { printf '%s\n' $2 | grep -qxF "$1"; }
# What the queue's timer starts: its ready jobs, rebuilt from scratch each install.
WANTS="$UNITS/session-ops-queue.service.wants"
rm -rf "$WANTS"
for job in $QUEUED; do
    if listed "$job" "$READY"; then
        install -d -m 755 "$WANTS"
        ln -s "$UNITS/session-ops@.service" "$WANTS/session-ops@$job.service"
    fi
done
systemctl daemon-reload

# A job removed from jobs.toml, whose env file was emptied, or now queued, loses its timer.
for link in "$UNITS"/timers.target.wants/session-ops@*.timer; do
    [ -L "$link" ] || continue
    job=${link##*/session-ops@}
    job=${job%.timer}
    if ! listed "$job" "$READY" || listed "$job" "$QUEUED"; then
        systemctl disable --now "session-ops@$job.timer" >/dev/null
        echo "disabled session-ops@$job.timer (no longer a ready job with a timer of its own)"
    fi
done
for job in $READY; do
    if listed "$job" "$QUEUED"; then
        echo "queued session-ops@$job.service"
    else
        systemctl enable --now "session-ops@$job.timer" >/dev/null
        echo "enabled session-ops@$job.timer"
    fi
done
if [ -d "$WANTS" ]; then
    systemctl enable --now session-ops-queue.timer >/dev/null
    echo "enabled session-ops-queue.timer"
else
    systemctl disable --now session-ops-queue.timer 2>/dev/null || true
fi
for job in $("$OPS" list --not-ready); do
    echo "not enabled: session-ops@$job (its env file is empty)"
done
if [ -s "$ETC/zendesk.env" ]; then
    systemctl enable zendesk-relay.service >/dev/null
    # A relay that hit its start limit refuses `start` until the limit is cleared.
    systemctl reset-failed zendesk-relay.service 2>/dev/null || true
    systemctl try-restart zendesk-relay.service
    systemctl start zendesk-relay.service
    echo "running zendesk-relay.service"
else
    echo "not enabled: zendesk-relay.service ($ETC/zendesk.env is empty)"
fi

if [ ! -s "$ETC/alerts.env" ]; then
    cat >&2 <<EOF

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! $ETC/alerts.env IS EMPTY.
!! OnFailure backstop and silence checker disabled until it sets
!! ALERT_DISCORD_WEBHOOK_URL: a job that is killed, times out, cannot reach
!! Discord or stops running is reported nowhere. Fill it, then run install.sh again.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
EOF
fi
