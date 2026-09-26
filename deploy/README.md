# Self-hosted deployment

Every job in [`jobs.toml`](../src/session_ops/jobs.toml) and both webhook endpoints run
on one machine, from one root-owned clone at `/opt/session-ops`;
[`install.sh`](install.sh) refuses to run from anywhere else.

| Unit | What it is |
| --- | --- |
| `session-ops@<job>.timer` → `.service` | One per job; a generated drop-in sets its account, env files and schedule. |
| `zendesk-relay.service` | Always on, `127.0.0.1:8080`: Zendesk's `claude:` note webhooks. |
| `crowdin-relay.service` | Always on, `127.0.0.1:8081`: Crowdin's suggestion webhooks. |
| `session-ops-alert@.service` | Every unit's `OnFailure=` backstop; see [session-ops-silence](../docs/jobs/session-ops-silence.md). |

`session-ops list` shows the jobs and schedules; `session-ops run <job> [--dry-run]
[-- job arguments]` runs one as its timer does.

| On disk | Where | Kept |
| --- | --- | --- |
| A run's scratch | its own `/tmp` | until the unit stops |
| State (dedup files, open slots) | `/var/lib/session-ops/<job>/` | always |
| Success stamps | `/var/lib/session-ops/stamps/` | always |
| Copies of a run | `/var/lib/session-ops/<job>/runs/<stamp>/` | 14 days |
| Caches | `/var/cache/session-ops/<job>/` | deletable any time |

## Host requirements

- Linux with systemd 252+ and Python 3.12+ at `/usr/bin/python3`; `git`;
  [uv](https://docs.astral.sh/uv/) on root's `PATH`.
- The Claude Code CLI installed by, and logged in as, `zendesk` (below).
- Always on. A public DNS name, 80 and 443 reachable, and nginx with certbot already
  managing its certificates.
- Few root holders: root here holds a Zendesk token that can comment on any ticket, a
  Claude Code login and a GitHub App key that pushes to the platform repos.

## Install

As root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
git clone https://github.com/session-foundation/session-shared-scripts /opt/session-ops
/opt/session-ops/deploy/install.sh
```

It creates the accounts, the venv, every env file (empty, `0600 root`) and the units,
and enables each job whose env files have content, disabling any other. Fill the
[env files](#secrets) and run it again; it warns for as long as `alerts.env` is empty,
since the backstop and the silence checker are off until then.

Then, once:

```bash
# runuser resets neither HOME nor PATH, so both are spelled out.
runuser -u zendesk -- env HOME=/home/zendesk sh -c 'curl -fsSL https://claude.ai/install.sh | bash'
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude   # then /login

# TLS, after DNS points here. First install only: certbot then rewrites the live file.
cp /opt/session-ops/deploy/nginx-webhooks.conf /etc/nginx/sites-available/webhooks.session.codes
ln -s /etc/nginx/sites-available/webhooks.session.codes /etc/nginx/sites-enabled/
nginx -t                                   # do not reload yet
certbot --nginx --redirect -d webhooks.session.codes
nginx -t && systemctl reload nginx
```

### Migrating from the per-job units

From a host that ran `zendesk-digest.timer` and `github-prs-digest.timer` out of
`/opt/zendesk`: run the new clone's `install.sh`. It stops and disables the old units,
copies `/etc/zendesk/env`, `/etc/github-prs/env`, both `seen.json` and
`house_answers.json` where the new file does not exist yet, and removes the old units.
Then:

```bash
"${EDITOR:-nano}" /etc/session-ops/zendesk.env
#   ZENDESK_HOUSE_ANSWERS=/var/lib/session-ops/zendesk-digest/house_answers.json
"${EDITOR:-nano}" /etc/session-ops/alerts.env    # created empty: ALERT_DISCORD_WEBHOOK_URL=
diff /opt/zendesk/deploy/nginx-webhooks.conf /opt/session-ops/deploy/nginx-webhooks.conf
/opt/session-ops/deploy/install.sh
systemctl list-timers 'session-ops@*'
# once both digests have run from the new units:
rm -rf /opt/zendesk /opt/github-prs /etc/zendesk /etc/github-prs /var/lib/zendesk /var/lib/github-prs
```

### The Crowdin duplicate-translation report

Seed the open slots once. Until then the relay ignores deliveries and reconciliation
refuses to run, so every `crowdin-duplicates` timer run fails with the seed
instruction. About an hour, read-only, posts nothing:

```bash
systemd-run --pipe --wait -p User=crowdin -p EnvironmentFile=/etc/session-ops/crowdin.env \
  -p StateDirectory=session-ops/crowdin-duplicates \
  /opt/session-ops/.venv/bin/crowdin-reconcile-duplicates --seed \
  --state /var/lib/session-ops/crowdin-duplicates/duplicates.json
```

Add the `location ^~ /crowdin/suggestions/` block of `nginx-webhooks.conf` to the live
file (see [Updating](#updating)). In Crowdin, project → Integrations → Webhooks → Add:
URL `https://webhooks.session.codes/crowdin/suggestions/<CROWDIN_WEBHOOK_SECRET>`, POST,
`application/json`, events *Suggestion added, updated, deleted, approved and disapproved*.

## Secrets

A `#` starts a comment only as a line's first character; a trailing one becomes part
of the value. A relay reads its file at start, so restart it after an edit. To see
what a unit loads (`systemctl show -p Environment` omits `EnvironmentFile=`):

```bash
systemd-run --pipe --wait --uid=zendesk -p EnvironmentFile=/etc/session-ops/zendesk.env env | grep -vi token
```

### `/etc/session-ops/zendesk.env`

```sh
# Not HOME, which systemd sets from the account. PATH replaces systemd's default.
PATH=/home/zendesk/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Optional: a one-year token from `claude setup-token` on a machine with a browser,
# same subscription. Without it the interactive login expires within weeks.
#CLAUDE_CODE_OAUTH_TOKEN=
ZENDESK_SUBDOMAIN=
# Authors every comment the reply flow posts.
ZENDESK_EMAIL=
ZENDESK_API_TOKEN=
# The triage channel: the digest, the review tally and this job's failures.
ZENDESK_DISCORD_WEBHOOK_URL=
# The secret on the Zendesk webhook. Empty refuses every note webhook.
ZENDESK_WEBHOOK_SECRET=
# Optional. Zendesk user ids allowed to drive `claude:` notes; empty is any agent or admin.
#ZENDESK_NOTE_AUTHORS=
# Optional. Grounds `claude: draft` and is all of `claude: explain`. It holds customer
# text, so it is not in the repo; copy it by hand:
#     install -o zendesk -g zendesk -m 640 house_answers.json /var/lib/session-ops/zendesk-digest/
#ZENDESK_HOUSE_ANSWERS=/var/lib/session-ops/zendesk-digest/house_answers.json
# Optional. A non-customer-visible multi-line text ticket field (its id is in its URL)
# that gets an English rendering of each non-English ticket the digest posts.
#ZENDESK_ENGLISH_FIELD_ID=
# The whole `claude:` note path, writing nothing to Zendesk. Set it for the first deploy.
#RELAY_DRY_RUN=1
```

### `/etc/session-ops/github-prs.env`

```sh
# Read-only, no scope: public repositories only.
GITHUB_PRS_TOKEN=
GITHUB_PRS_DISCORD_WEBHOOK_URL=
# Optional. Where this job's failures go instead of the channel above.
#ALERT_DISCORD_WEBHOOK_URL=
```

### `/etc/session-ops/alerts.env`

```sh
# Where the OnFailure backstop and the silence checker post. Empty disables both.
ALERT_DISCORD_WEBHOOK_URL=
```

### `/etc/session-ops/crowdin.env`

```sh
# Read-only. Scopes: Projects, Source files & strings, Translations.
CROWDIN_API_TOKEN=
CROWDIN_DISCORD_WEBHOOK_URL=
# The last path segment of the URL given to Crowdin; empty refuses every delivery.
# openssl rand -hex 32
CROWDIN_WEBHOOK_SECRET=
# The relay prints what it would post.
#CROWDIN_RELAY_DRY_RUN=1
```

### `/etc/session-ops/publish.env`

For `crowdin-sync`, `snode-list` and `release-stats`, which publish only as the GitHub
App and need its key at `/etc/session-ops/github-app.pem`.

```sh
# The commits' author, "Name <email>".
PUBLISH_GIT_AUTHOR=
GITHUB_APP_ID=
# Where their failures go.
ALERT_DISCORD_WEBHOOK_URL=
```

The App: organisation settings → Developer settings → GitHub Apps → New. No webhook;
repository permissions *Contents* and *Pull requests*, read and write, nothing else.
Install it on session-android, session-ios and session-localization, generate a
private key, then:

```bash
install -m 600 /dev/stdin /etc/session-ops/github-app.pem < downloaded-key.pem
"${EDITOR:-nano}" /etc/session-ops/publish.env    # GITHUB_APP_ID=
```

## Verifying, in order

**1. The relay.** An unsigned request gets 401; a 404 means the route is not deployed.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:8080/zendesk/notes \
  -H 'Content-Type: application/json' -d '{"ticket_id":"1"}'   # expect 401
curl -sS 127.0.0.1:8080/healthz                                # expect {"ok":true}
```

**2. The Claude CLI, as the units run it.** An `is_error` envelope naming
authentication means the login or token is dead.

```bash
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude --version
# The running relay's HOME and PATH; an env file edit shows only after a restart.
tr '\0' '\n' < /proc/"$(systemctl show -p MainPID --value zendesk-relay)"/environ | grep -E '^(HOME|PATH)='
# With CLAUDE_CODE_OAUTH_TOKEN set, this tests the token rather than the login:
systemd-run --pty --uid=zendesk -p EnvironmentFile=/etc/session-ops/zendesk.env \
  --setenv=HOME=/home/zendesk /home/zendesk/.local/bin/claude \
  --print --model claude-sonnet-5 --output-format json 'reply with OK'
# Under the relay's sandbox, which --version does not exercise:
systemd-run --pty --uid=zendesk --setenv=HOME=/home/zendesk \
  -p ProtectSystem=strict -p ProtectHome=tmpfs -p BindPaths=/home/zendesk \
  -p ReadWritePaths=/home/zendesk -p MemoryDenyWriteExecute=yes \
  -p SystemCallFilter=@system-service \
  /home/zendesk/.local/bin/claude --print --model claude-sonnet-5 'reply with OK'
```

**3. The Zendesk digest.** `systemctl start session-ops@zendesk-digest.service`, then
`journalctl -fu session-ops@zendesk-digest`.

**4. The timers.** `systemctl list-timers 'session-ops@*'`. Check a changed `schedule`
with `systemd-analyze calendar "Mon 13:00 Australia/Melbourne"` (the zone belongs in
the expression; systemd ignores a `Timezone=` key), then re-run `install.sh`.

**5. Through Zendesk.** With the webhook and trigger set up
([zendesk-relay](../docs/jobs/zendesk-relay.md#zendesk-setup)), write `claude: english`
as a private note on a throwaway ticket with a public, ideally non-English, comment and
watch `journalctl -fu zendesk-relay`. A 401 there means the HMAC secret is wrong.

**6. The failure path.** `systemctl start session-ops-alert@test.service` posts to the
alerts channel. The backstop quotes the failed unit's journal, so check it can read it
(an empty `SupplementaryGroups=` is a stale unit: re-run `install.sh`). The last
command prints the alert a job's own failure would post:

```bash
systemctl show session-ops-alert@test.service -p SupplementaryGroups   # systemd-journal
runuser -u sessionops -- journalctl -u session-ops@zendesk-digest.service -n 1 --no-pager
systemd-run --pipe --wait -p User=ghdigest -p EnvironmentFile=/etc/session-ops/github-prs.env \
  /opt/session-ops/.venv/bin/session-ops run github-prs-digest --dry-run -- --org does-not-exist
```

**7. The pull request digest.** A dry run as `ghdigest` with its env file, posting
nothing; it has none of the template's sandboxing, which only the real runs exercise.
Then start the unit twice: the second run proves the dedup state.

```bash
systemd-run --pty -p User=ghdigest -p EnvironmentFile=/etc/session-ops/github-prs.env \
  /opt/session-ops/.venv/bin/session-ops run github-prs-digest --dry-run
systemctl start session-ops@github-prs-digest.service    # twice
journalctl -u session-ops@github-prs-digest -n 5 --no-pager
#   …: 0 new, 0 changed since last reported, N unchanged (skipped).
ls -l /var/lib/session-ops/github-prs-digest/seen.json
```

A second run that reports everything again wrote no state: check
`systemctl show session-ops@github-prs-digest -p StateDirectory`.

**8. The silence checker.** Prints each job's last success and the alert it would post:

```bash
systemd-run --pipe --wait -p User=sessionops -p EnvironmentFile=/etc/session-ops/alerts.env \
  /opt/session-ops/.venv/bin/session-ops-silence --dry-run
ls -l /var/lib/session-ops/stamps/     # one file per job that has succeeded
```

**9. The Crowdin relay and reconciliation.** A suggestion typed in the Crowdin editor
should then log a line in `journalctl -fu crowdin-relay`.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:8081/crowdin/suggestions/wrong \
  -H 'Content-Type: application/json' -d '{}'                       # expect 404
. /etc/session-ops/crowdin.env; curl -sS -X POST \
  "127.0.0.1:8081/crowdin/suggestions/$CROWDIN_WEBHOOK_SECRET" \
  -H 'Content-Type: application/json' -d '{}'                       # expect "checks":0
systemd-run --pipe --wait -p User=crowdin -p EnvironmentFile=/etc/session-ops/crowdin.env \
  -p StateDirectory=session-ops/crowdin-duplicates \
  /opt/session-ops/.venv/bin/session-ops run crowdin-duplicates --dry-run -- --locales de
```

## Updating

```bash
git -C /opt/session-ops pull
/opt/session-ops/deploy/install.sh     # systemd runs the installed units, not the clone
```

certbot owns the live nginx file, so a change to `nginx-webhooks.conf` is made by hand:

```bash
diff /opt/session-ops/deploy/nginx-webhooks.conf /etc/nginx/sites-available/webhooks.session.codes
"${EDITOR:-nano}" /etc/nginx/sites-available/webhooks.session.codes
nginx -t && systemctl reload nginx
```

## Moving the Zendesk digest to another Discord channel

Ask that server's admins for a webhook created in the target channel (Channel
Settings → Integrations → Webhooks). Moving `seen.json` aside makes the first digest
there report the whole window rather than skip what the old channel saw.

```bash
"${EDITOR:-nano}" /etc/session-ops/zendesk.env   # ZENDESK_DISCORD_WEBHOOK_URL; RELAY_DRY_RUN=1 at first
mv /var/lib/session-ops/zendesk-digest/seen.json{,.old}
systemctl restart zendesk-relay
systemctl start session-ops@zendesk-digest.service
```

## If this host goes down

Nothing else runs these jobs; recovery is fixing this host. A reboot is harmless: a
killed run has written no state, and `Persistent=yes` runs a missed schedule once.

- **Replies stop.** Unanswered `claude:` notes keep the `claude-queued` tag, so
  `tags:claude-queued` lists them. A re-run never answers a command twice.
- **The pull request digest is late, not lost:** its window reaches back to the start
  of its last fully posted run.
- **The Zendesk digest's window is a fixed 72 hours.** Once more than 72 hours pass
  between runs (a weekend already takes 72), tickets whose requester last wrote in the
  gap are never reported; they are still in Zendesk. Re-run with a window that covers
  the gap, in hours:

  ```bash
  systemd-run --pipe --wait -p User=zendesk -p EnvironmentFile=/etc/session-ops/zendesk.env \
    /opt/session-ops/.venv/bin/session-ops run zendesk-digest -- --window-hours 168
  ```
- **A lost `seen.json`** re-reports that digest's window once.
