# Self-hosted deployment

Every job in [`jobs.toml`](../src/session_ops/jobs.toml) and both webhook endpoints, on
one machine, from one clone at `/opt/session-ops` and the one venv in it.

| Unit | What it is |
| --- | --- |
| `session-ops@<job>.timer` → `.service` | One per job in `jobs.toml`: the template holds the hardening, a generated drop-in the account, env file and schedule. |
| `zendesk-relay.service` | Always on. The endpoint Zendesk posts `claude:` note webhooks to. |
| `crowdin-relay.service` | Always on. The endpoint Crowdin posts suggestion webhooks to. |
| `session-ops-alert@.service` | Pulled in by every unit's `OnFailure=`; see below. |

`session-ops list` shows the jobs, `session-ops run <job> [--dry-run]` runs one the way
its timer does, and [`install.sh`](install.sh) installs or updates everything.

## Failure alerts

Three layers, so that a job cannot fail, or stop, without Discord hearing of it:

1. **The run itself.** `session-ops run` reports its own failure: job, host, the step it
   was on, one sentence of error with secrets scrubbed, each target's result for a job
   with several, and the commands to read the journal and re-run it. It then records
   its invocation id in `/var/lib/session-ops/<job>/alerted`.
2. **`OnFailure=`**, through `session-ops-alert@`, for what a run cannot report: killed,
   timed out, a missing env file, Discord unreachable. It stays quiet for an invocation
   the run already reported, and quotes the unit's last journal line otherwise.
3. **Silence.** `session-ops@session-ops-silence` posts every scheduled job whose last
   success, stamped by the template's `ExecStartPost=`, is older than its
   `max_age_hours`.

A run's alert goes to `ALERT_DISCORD_WEBHOOK_URL` when its env file sets one, else to
the job's own channel. The backstop and the silence checker use `alerts.env`.
`ALERT_DISCORD_ROLE_ID` in either is mentioned.

## Disk

| What | Where | Lifetime |
| --- | --- | --- |
| A run's scratch: clones, downloads, generated files | its own `/tmp` (`PrivateTmp=`) | gone when the unit stops |
| State: dedup files, open slots, stamps | `/var/lib/session-ops/<job>/` | kept; written only once a run's output was delivered |
| Copies of a run kept for debugging | `/var/lib/session-ops/<job>/runs/<stamp>/` | 14 days, by `systemd-tmpfiles` |
| Caches | `/var/cache/session-ops/<job>/` | deletable at any time |

The box reboots often, and that is harmless: a run killed mid-flight has written
nothing, and `Persistent=yes` runs a missed schedule once the box is back.

## Host requirements

- Linux with systemd 252 or newer (the timer needs a timezone in `OnCalendar=`), and Python 3.12+
- [uv](https://docs.astral.sh/uv/) on root's `PATH`. The venv must use the system
  Python, not one uv downloads: that would live under root's home, which
  `ProtectHome=` hides from every unit.
- `git`, for the jobs that publish to the platform repos.
- **The Claude Code CLI installed and logged in as `zendesk`.** Both Claude calls go
  through it — the digest's classification and the reply flow's translation — so its
  login is the only Claude credential this box holds. It must be installed *by* that
  account: the CLI keeps its binary, its login and its cache under `$HOME`, which is
  `/home/zendesk`. A root install lands in `/root/.local`, which is `0700` and
  unreadable to the service.
- **Always on.** A workstation is not a candidate: user timers stop at logout unless
  lingering is enabled, and a sleeping laptop silently skips the digest.
- A public DNS name resolving here, with **80 and 443 reachable** — 80 for certbot's
  HTTP-01 challenge, which is what the existing nginx setup already uses.
- **nginx already installed**, with certbot managing its certificates. This adds one
  server block to it rather than a second web server; two would fight over :443 and
  take the host's other sites down with them.

Note who else holds root. This box becomes custodian of a Zendesk API token that can
write a public comment to any ticket, of a logged-in Claude Code session, and of a
token that can push to the platform repos.

## Install

Run as root. Every command below assumes it; prefix with `sudo` if you are not.

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
git clone https://github.com/session-foundation/session-shared-scripts /opt/session-ops
/opt/session-ops/deploy/install.sh
```

`install.sh` creates the accounts, the venv, every env file (empty, `0600 root`: systemd
reads them before dropping privileges, so no job's account can read another's
secrets), the stamps directory and the units, and enables each job whose env file has
content. Fill the env files (contents under [Secrets](#secrets)) and run it again.

Then, once, the two things it leaves to you:

```bash
# The Claude Code CLI, installed and logged in AS zendesk. `runuser -u` resets neither
# HOME nor PATH, so both are spelled out: without the explicit HOME the login lands
# in root's home, and a bare `claude` resolves against root's PATH, which reports
# `Permission denied` for a binary that is installed perfectly well.
runuser -u zendesk -- env HOME=/home/zendesk sh -c 'curl -fsSL https://claude.ai/install.sh | bash'
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude

# TLS, through the nginx and certbot already on this host. Point DNS at the
#    box first or certbot has nothing to validate against. The shipped config is
#    HTTP-only on purpose — certbot adds the TLS directives to that same server
#    block, so no location needs moving.
#
#    Validate before certbot, activate after. certbot reloads nginx itself while
#    it answers the challenge, so reloading beforehand would only widen the moment
#    the proxy is reachable over plain HTTP. --redirect is explicit so that once
#    TLS is in place HTTP can only redirect, never proxy an unencrypted POST.
cp /opt/session-ops/deploy/nginx-webhooks.conf /etc/nginx/sites-available/webhooks.session.codes
ln -s /etc/nginx/sites-available/webhooks.session.codes /etc/nginx/sites-enabled/
nginx -t                                          # validate; do not reload yet
certbot --nginx --redirect -d webhooks.session.codes
nginx -t && systemctl reload nginx                # validate what certbot wrote
```

> **This `cp` is first-install only.** certbot rewrites that file in place to add the
> TLS directives, so copying the repo's copy over it a second time silently reverts
> the site to HTTP-only. To pick up a change to `nginx-webhooks.conf` on a host that
> is already serving — a new `location`, say — diff the two and edit the live file:
>
> ```bash
> diff /opt/session-ops/deploy/nginx-webhooks.conf \
>      /etc/nginx/sites-available/webhooks.session.codes
> "${EDITOR:-nano}" /etc/nginx/sites-available/webhooks.session.codes
> nginx -t && systemctl reload nginx
> ```

### Moving from the per-job units

A host that ran the digests from `/opt/zendesk`, with `zendesk-digest.timer` and
`github-prs-digest.timer`: `install.sh` copies `/etc/zendesk/env`,
`/etc/github-prs/env` and the digests' `seen.json` and `house_answers.json` into the new
layout, only where the new file does not exist yet, then disables and removes the old
units. After the install above:

```bash
"${EDITOR:-nano}" /etc/session-ops/zendesk.env
#   ZENDESK_HOUSE_ANSWERS=/var/lib/session-ops/zendesk-digest/house_answers.json
diff /opt/zendesk/deploy/nginx-webhooks.conf /opt/session-ops/deploy/nginx-webhooks.conf
/opt/session-ops/deploy/install.sh
systemctl list-timers 'session-ops@*'
# once both digests have run from the new units:
rm -rf /opt/zendesk /opt/github-prs /etc/zendesk /etc/github-prs /var/lib/zendesk /var/lib/github-prs
```

### The Crowdin duplicate-translation report

Seed the open slots once before either half runs; the relay does nothing until they
exist. About an hour, read-only, and it posts nothing:

```bash
systemd-run --pipe --wait -p User=crowdin -p EnvironmentFile=/etc/session-ops/crowdin.env \
  -p StateDirectory=session-ops/crowdin-duplicates \
  /opt/session-ops/.venv/bin/crowdin-reconcile-duplicates --seed \
  --state /var/lib/session-ops/crowdin-duplicates/duplicates.json
```

Add the `location ^~ /crowdin/suggestions/` block from `nginx-webhooks.conf` to the live
nginx file, then `nginx -t && systemctl reload nginx`. Last, in Crowdin: project →
Integrations → Webhooks → Add, URL
`https://webhooks.session.codes/crowdin/suggestions/<CROWDIN_WEBHOOK_SECRET>`, request
type POST, content type `application/json`, events *Suggestion added, updated, deleted,
approved and disapproved*.

## Secrets

`/etc/session-ops/zendesk.env`. `EnvironmentFile=` needs no code change because every
script already reads its configuration from the environment.

```sh
# systemd only treats a # as a comment when it is the FIRST character on a line.
# An inline one becomes part of the value, so every comment here sits above its
# variable — a trailing "# what this is" would be silently appended to your token
# and Zendesk would answer 401.

# HOME is deliberately absent: systemd sets it from the account database for units
# with User=, so it follows `useradd --home` and cannot drift out of sync with it.
# PATH is not absent: systemd's default does not cover a per-user install, so without
# this the units cannot find `claude` at all. Setting it replaces that default rather
# than extending it, which is why the standard directories are repeated.
PATH=/home/zendesk/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Optional, and the one to use on a host nobody logs into. `claude setup-token` on a
# machine with a browser mints a token that lasts a year, against the same
# subscription and the same billing as the interactive login — it is not an API key.
# Without it the CLI runs on the login `claude` was signed in with, whose refresh
# token hard-expires in weeks and takes the digest and the `claude:` notes with it
# when it does. Paste the token here; it is printed once and saved nowhere.
#CLAUDE_CODE_OAUTH_TOKEN=

ZENDESK_SUBDOMAIN=
# Authors every comment the reply flow posts.
ZENDESK_EMAIL=
ZENDESK_API_TOKEN=

# Where the digest, the review tally and the failure alerts all land. The real
# triage channel, not a test server: a webhook is bound to the channel it was
# created in, so this one value decides where everything goes.
ZENDESK_DISCORD_WEBHOOK_URL=

# Verifies Zendesk's webhook signatures, for the `claude:` private-note route.
# Empty refuses every note webhook: a URL that writes public comments must not
# default to open. Same value as the secret on the Zendesk webhook itself.
ZENDESK_WEBHOOK_SECRET=
# Optional. Comma-separated Zendesk user ids allowed to drive `claude:` notes.
# Empty means any agent or admin, which is already everybody who can write a
# private note. The agent/admin role check applies either way.
#ZENDESK_NOTE_AUTHORS=
# Optional. Path to the house-answer file: what support actually replied to each
# kind of problem, per platform. It grounds `claude: draft` and is the whole of
# `claude: explain`; unset, drafting works as it does without it and explain says
# there is nothing to look up. NOT in the repo — it carries ticket ids and customer
# text and the repo is public — so copy it here by hand:
#     install -o zendesk -g zendesk -m 640 house_answers.json /var/lib/session-ops/zendesk-digest/
#ZENDESK_HOUSE_ANSWERS=/var/lib/session-ops/zendesk-digest/house_answers.json

# Optional. The numeric id of a multi-line text ticket field in Zendesk; the digest
# renders every non-English ticket it is about to post into English there — both
# sides of the conversation, timestamped — so an agent opening the ticket can read
# it. Until this is set the step does nothing. Settings -> Ticket Fields ->
# Multi-line text, not customer-visible, then read the id off the field's URL.
#ZENDESK_ENGLISH_FIELD_ID=

# Uncomment to run the whole `claude:` note path and write nothing to Zendesk.
#RELAY_DRY_RUN=1
```

Check what systemd actually loads from it, rather than what you think you wrote.
`systemctl show -p Environment` will not do: it lists only `Environment=` lines from
the unit and nothing from `EnvironmentFile=`.

```bash
systemd-run --pipe --wait --uid=zendesk -p EnvironmentFile=/etc/session-ops/zendesk.env env | grep -vi token
```

Set `RELAY_DRY_RUN=1` for the first deployment. The whole `claude:` note path runs —
webhook, command parsing, composing, translation — and the Zendesk writes are
skipped.

### `/etc/session-ops/github-prs.env`

Separate from the Zendesk file: a token that reads the org has no business in the
environment of the relay, the one process here reachable from the internet.

```sh
# Read-only, and no scope at all: the digest reads public repositories only. Nothing
# here ever writes to GitHub.
GITHUB_PRS_TOKEN=

# The channel the digest posts to. A webhook is bound to the channel it was created
# in, so this one value decides where the digest goes.
GITHUB_PRS_DISCORD_WEBHOOK_URL=

# Optional. Where this job's failures go; without it, the channel above.
#ALERT_DISCORD_WEBHOOK_URL=
```

### `/etc/session-ops/alerts.env`

```sh
# Where the backstop and the silence checker post. Any channel whose readers can act
# on a job that failed or stopped.
ALERT_DISCORD_WEBHOOK_URL=
# Optional. A role to mention in every alert.
#ALERT_DISCORD_ROLE_ID=
```

### `/etc/session-ops/crowdin.env`

```sh
# Read-only: the relay is reachable from the internet, and nothing here writes to
# Crowdin. Scopes: Projects, Source files & strings, Translations (read).
CROWDIN_API_TOKEN=

# The channel new and resolved slots go to.
CROWDIN_DISCORD_WEBHOOK_URL=

# The last path segment of the webhook URL given to Crowdin, which signs nothing.
# Empty refuses every delivery. Generate with: openssl rand -hex 32
CROWDIN_WEBHOOK_SECRET=

# Uncomment for the first deliveries: the relay prints what it would post.
#CROWDIN_RELAY_DRY_RUN=1
```

## Verifying, in order

**1. Locally, before Zendesk knows the address.** An unsigned request must be refused:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  127.0.0.1:8080/zendesk/notes \
  -H 'Content-Type: application/json' -d '{"ticket_id":"1"}'   # expect 401
curl -sS 127.0.0.1:8080/healthz                                # expect {"ok":true}
```

401 rather than 404 is the thing to check: 404 means the route is not deployed.

**1b. The Claude CLI, as the service user.** Both Claude calls shell out to it, and
this is the step most likely to be wrong after a fresh install — a login that landed
in the wrong `$HOME`, or a binary the units cannot reach, fails only when the digest
next runs.

```bash
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude --version
tr '\0' '\n' < /proc/"$(systemctl show -p MainPID --value zendesk-relay)"/environ | grep -E '^(HOME|PATH)='
```

The second line reads the running relay's own environment, so it also tells you
whether an edit to `/etc/session-ops/zendesk.env` has reached it yet — it does not until the unit
is restarted.

Both the explicit `HOME` and the absolute path are load-bearing: `runuser -u` resets
neither, so a bare `claude` there is resolved against root's `PATH` and reports
`Permission denied` for an install that is fine.

With `CLAUDE_CODE_OAUTH_TOKEN` set, that check tests the wrong credential — it reads
the interactive login, which the token outranks. Test what the unit will actually use:

```bash
systemd-run --pty --uid=zendesk -p EnvironmentFile=/etc/session-ops/zendesk.env \
  --setenv=HOME=/home/zendesk /home/zendesk/.local/bin/claude \
  --print --model claude-sonnet-5 --output-format json 'reply with OK'
```

An `is_error` envelope naming authentication means the token is wrong or expired; the
digest reports the same message to Discord once it runs.

That covers the install. The sandbox is the other half, and no amount of reading the
unit file settles it — `ProtectHome=tmpfs`, `MemoryDenyWriteExecute=` and
`SystemCallFilter=` each have a plausible way to break a JIT-compiled CLI. Rehearse
the relay's exact confinement, with a real inference rather than `--version`:

```bash
systemd-run --pty --uid=zendesk --setenv=HOME=/home/zendesk \
  -p ProtectSystem=strict -p ProtectHome=tmpfs -p BindPaths=/home/zendesk \
  -p ReadWritePaths=/home/zendesk -p MemoryDenyWriteExecute=yes \
  -p SystemCallFilter=@system-service \
  /home/zendesk/.local/bin/claude --print --model claude-sonnet-5 'reply with OK'
```

**2. The digest, by hand.** `systemctl start session-ops@zendesk-digest.service` and
watch `journalctl -fu session-ops@zendesk-digest`. It prints ticket counts and outcomes,
never content.

**3. The timers fire when you expect.** `systemctl list-timers 'session-ops@*'` — and
if you change a `schedule` in `jobs.toml`, check it with
`systemd-analyze calendar "Mon..Fri 10:00 Australia/Melbourne"` and re-run
`install.sh`. There is no `Timezone=` key and systemd ignores one silently.

**4. Through Zendesk.** With the webhook and the trigger in place (see
[zendesk-relay](../docs/jobs/zendesk-relay.md#zendesk-setup)), write `claude: english` as a private note on a
throwaway ticket and watch `journalctl -fu zendesk-relay`. `english` is the read-only verb, so
nothing can reach a customer if the wiring is wrong.

The ticket needs at least one **public** comment, ideally not in English. `english`
translates the conversation, and a ticket carrying only private notes has nothing to
work on — it answers "there are no public comments on this ticket to translate",
which proves the wiring but exercises none of the model path.

The one thing only this step can prove is that the relay's HMAC matches Zendesk's. If
it does not, every note logs a 401 and nothing happens.

**5. The failure path.** `systemctl start session-ops-alert@test.service` should put a
line in the alerts channel, and a dry run shows what a job's own alert would say:

```bash
systemd-run --pipe --wait -p User=ghdigest -p EnvironmentFile=/etc/session-ops/github-prs.env \
  /opt/session-ops/.venv/bin/session-ops run github-prs-digest --dry-run -- --org does-not-exist
```

The alert quotes the failed unit's last journal line, which is what tells the channel
whether the job broke or the Claude Code login simply expired — the two look identical
otherwise, and only one of them is fixed by re-running anything. That needs
`SupplementaryGroups=systemd-journal` on the alert unit, so check it reached systemd
and that the account can actually read a journal:

```bash
systemctl show session-ops-alert@test.service -p SupplementaryGroups
runuser -u sessionops -- journalctl -u session-ops@zendesk-digest.service -n 1 --no-pager
```

An empty `SupplementaryGroups=` means the installed unit is the old copy — see
Updating. A permission error from the second command costs the excerpt and nothing
else: the alert still sends, with the message it always sent.

```sh
uv run python -m unittest discover -s tests/monitor -t .     # the alert's own tests
```
**6. The pull request digest.** A dry run under the unit's own confinement renders the
digest and posts nothing. `systemd-run` rather than `runuser` because the token then
comes from the environment file rather than an argument every process on the box can
read out of `ps`:

```bash
systemd-run --pty -p User=ghdigest -p EnvironmentFile=/etc/session-ops/github-prs.env \
  /opt/session-ops/.venv/bin/session-ops run github-prs-digest --dry-run
```

Then for real: `systemctl start session-ops@github-prs-digest.service`, and
`systemctl list-timers session-ops@github-prs-digest` (expect the next weekday, not
tomorrow).

Start it twice. The second run is the one that proves the dedup state: it should report
everything, then nothing, and say so.

```bash
journalctl -u session-ops@github-prs-digest -n 5 --no-pager   # "N new, 0 changed, N unchanged"
ls -l /var/lib/session-ops/github-prs-digest/seen.json
```

A second run that reports everything again means the state was not written — check that
`StateDirectory=` reached systemd with
`systemctl show session-ops@github-prs-digest -p StateDirectory`.

**7. The silence checker.** A dry run prints every job's last success and the alert
it would post, and writes no state:

```bash
systemd-run --pipe --wait -p User=sessionops -p EnvironmentFile=/etc/session-ops/alerts.env \
  /opt/session-ops/.venv/bin/session-ops-silence --dry-run
ls -l /var/lib/session-ops/stamps/     # one file per job that has succeeded since
```

`systemctl start session-ops-alert@test.service` checks its failure path.

**8. The Crowdin relay and reconciliation.** Without the secret the route must not
exist, and with it an empty delivery is acknowledged:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:8081/crowdin/suggestions/wrong \
  -H 'Content-Type: application/json' -d '{}'                       # expect 404
. /etc/session-ops/crowdin.env; curl -sS -X POST \
  "127.0.0.1:8081/crowdin/suggestions/$CROWDIN_WEBHOOK_SECRET" \
  -H 'Content-Type: application/json' -d '{}'                       # expect "checks":0
```

A suggestion typed into the Crowdin editor should then log a line in
`journalctl -fu crowdin-relay`. A dry run of reconciliation for one locale:

```bash
systemd-run --pipe --wait -p User=crowdin -p EnvironmentFile=/etc/session-ops/crowdin.env \
  -p StateDirectory=session-ops/crowdin-duplicates \
  /opt/session-ops/.venv/bin/session-ops run crowdin-duplicates --dry-run -- --locales de
```

## Updating

```bash
git -C /opt/session-ops pull
/opt/session-ops/deploy/install.sh
```

`install.sh` rebuilds the venv from `uv.lock`, reinstalls the units, regenerates the
drop-ins from `jobs.toml` and restarts the relays; systemd reads the installed units,
not the checkout, so skipping it leaves a stale unit failing the way the old one did.
The one thing it cannot update is nginx, which certbot owns: a change to
`nginx-webhooks.conf` is made by hand in the live file.

Manual on purpose. Automating this would mean giving CI an SSH key to the box, which
is the coupling self-hosting was meant to remove.

## Pointing the digest at another Discord server

The Zendesk half does not move — replies are written on the ticket, not from Discord,
so nothing about the reply flow is involved. What moves is everything that identifies
the channel everything posts into. DNS and nginx stay exactly as they are.

One thing for the server's admins: **create a webhook in the target channel**
(Channel Settings → Integrations → Webhooks) and send the URL privately. A webhook is
bound to the channel it was created in, so the old one cannot reach the new one. No
application and no bot invite — the digest carries no interactive components, which is
the whole of what a webhook may not send.

Then, on the host:

```bash
"${EDITOR:-nano}" /etc/session-ops/zendesk.env   # ZENDESK_DISCORD_WEBHOOK_URL,
                                                # ZENDESK_WEBHOOK_SECRET, and
                                                # RELAY_DRY_RUN=1 for the first run
mv /var/lib/session-ops/zendesk-digest/seen.json{,.old}
systemctl restart zendesk-relay
systemctl start session-ops@zendesk-digest.service
```

Move `seen.json` aside or the first digest in the new channel says almost nothing —
dedup state is per ticket, not per channel, so everything already reported to the old
one stays suppressed. Moving it re-reports the window once, which is the noisy-but-
correct outcome the state file is built around.

## If this host goes down

Nothing else runs any of this — the scheduled jobs and the note webhook both live here
only, so recovery means fixing the host rather than failing over.

What that costs, in order of how much it matters:

- **Replies stop.** A `claude:` note gets no answer at all, and the ticket keeps the
  `claude-queued` tag the trigger added — so `tags:claude-queued` is the list of work
  the outage swallowed, and nothing is lost silently. A half-run cannot email a
  customer twice either: every outcome note carries a marker keyed on the commanding
  comment, and a re-run finds it and stops.
- **The digest is late, not lost.** `Persistent=yes` on the timer means a host that
  was down at 10:00 runs the digest once when it comes back, and the 72-hour window
  covers the gap.
- **The dedup state may be stale.** Losing either `seen.json` re-reports that digest's
  window once: noisy, never wrong.
- **The pull request digest is late, not lost**, on the same `Persistent=yes` as the
  Zendesk one, and its 72-hour window already covers a weekend's gap.

Failures that are not a whole-host outage report themselves; see
[Failure alerts](#failure-alerts).