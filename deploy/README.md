# Self-hosted deployment

Every job in [`jobs.toml`](../src/session_ops/jobs.toml) and the Zendesk webhook run on
one machine, from a root-owned clone at `/opt/session-ops`. [`install.sh`](install.sh)
does the work: accounts, venv, env files, units, timers, and migrating an older layout.

| Unit | What it is |
| --- | --- |
| `session-ops@<job>.timer` → `.service` | One per job; a generated drop-in sets its account, env files and schedule. |
| `session-ops-queue.timer` → `.service` | Starts the jobs in `jobs.toml`'s `[queue]`, which then run one at a time in its order. |
| `zendesk-relay.service` | Always on, `127.0.0.1:8080`: Zendesk's `claude:` note webhooks. |
| `session-ops-alert@.service` | Every unit's `OnFailure=` backstop; see [session-ops-silence](../docs/jobs/session-ops-silence.md). |

`session-ops list` shows the jobs; `session-ops run <job> [--dry-run] [-- job arguments]`
runs one as its timer does. State is under `/var/lib/session-ops/<job>/`, copies of a
run under its `runs/` for 14 days.

## Install

Needs systemd 252+, Python 3.11+ at `/usr/bin/python3`, `git`, and nginx with certbot.
As root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
git clone https://github.com/session-foundation/session-shared-scripts /opt/session-ops
/opt/session-ops/deploy/install.sh
```

Then fill the env files and run `install.sh` again. It enables each job once its env
files have content.

```bash
runuser -u zendesk -- env HOME=/home/zendesk sh -c 'curl -fsSL https://claude.ai/install.sh | bash'
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude   # then /login

cp /opt/session-ops/deploy/nginx-webhooks.conf /etc/nginx/sites-available/webhooks.session.codes
ln -s /etc/nginx/sites-available/webhooks.session.codes /etc/nginx/sites-enabled/
nginx -t && certbot --nginx --redirect -d webhooks.session.codes && nginx -t && systemctl reload nginx
```

Seed the Crowdin duplicates once, or reconciliation refuses to run. About ten minutes,
posting nothing:

```bash
systemd-run --pipe --wait -p User=crowdin -p EnvironmentFile=/etc/session-ops/crowdin.env \
  -p StateDirectory=session-ops/crowdin-duplicates -p StateDirectoryMode=0711 -p UMask=0077 \
  /opt/session-ops/.venv/bin/crowdin-reconcile-duplicates --seed --croql \
  --state /var/lib/session-ops/crowdin-duplicates/duplicates.json
```

From a host that ran the digests out of `/opt/zendesk`, `install.sh` copies their env
files and state over. Remove `/opt/zendesk`, `/etc/zendesk` and `/var/lib/zendesk`
(and the `github-prs` equivalents) once both digests have run from the new units.

## Secrets

Each `/etc/session-ops/<name>.env` has a commented `<name>.env.example` beside it,
installed from [`env/`](env/), saying what goes in it.

## Checking

```bash
systemctl list-timers 'session-ops*'
systemctl start session-ops@<job>.service && journalctl -fu session-ops@<job>
# Re-run one job by its own service: starting session-ops-queue.service again re-runs
# every queued job that has already finished today.
systemctl start session-ops-alert@test.service          # posts to the alerts channel
curl -sS -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:8080/zendesk/notes \
  -H 'Content-Type: application/json' -d '{"ticket_id":"1"}'   # expect 401
```

## Rehearsing on a spare host

Create `/etc/session-ops/rehearsal` before the first `install.sh`. Every job then runs
against live data without writing over production: pull requests come from
`rehearsal/…` branches, session-localization gets a pull request instead of a push, and
no Zendesk ticket is solved or written to. Point every webhook URL at your own
channels, set `RELAY_DRY_RUN=1`, and leave Zendesk's webhook on the production host.

To end it, stop the timers, then close the rehearsal pull requests:

```bash
for link in /etc/systemd/system/timers.target.wants/session-ops@*.timer; do
  [ -L "$link" ] && systemctl disable --now "${link##*/}"
done
systemctl disable --now session-ops-queue.timer zendesk-relay.service
for repo in session-android session-ios session-localization; do
  gh pr list -R "session-foundation/$repo" --state open --json number,headRefName \
    -q '.[] | select(.headRefName | startswith("rehearsal/")) | .number' |
    xargs -r -I{} gh pr close -R "session-foundation/$repo" {} --delete-branch
done
```

Remove the marker only to promote the host, once the production host is off.

## Updating

```bash
git -C /opt/session-ops pull && /opt/session-ops/deploy/install.sh
```

certbot owns the live nginx file: apply a change to `nginx-webhooks.conf` to
`/etc/nginx/sites-available/webhooks.session.codes` by hand, then
`nginx -t && systemctl reload nginx`.

## If this host goes down

A reboot is harmless: a killed run writes no state, and a missed schedule runs once.
Unanswered `claude:` notes keep `claude-queued`, and the PR digest reaches back to its
last full post. The Zendesk digest's window is a fixed 72 hours, so after a longer gap
re-run it with one that covers the gap:

```bash
systemd-run --pipe --wait -p User=zendesk -p EnvironmentFile=/etc/session-ops/zendesk.env \
  -p UMask=0077 /opt/session-ops/.venv/bin/session-ops run zendesk-digest -- --window-hours 168
```
