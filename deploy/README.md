# Self-hosted deployment

The Zendesk triage digest, the `claude:` note webhook and the contributor pull request
digest, all on one machine.

| Unit | What it is |
| --- | --- |
| `zendesk-relay.service` | Always on. The HTTPS endpoint Zendesk posts note webhooks to. |
| `zendesk-digest.timer` → `.service` | Weekday mornings. Resolves positive reviews, then posts the digest. |
| `github-prs-digest.timer` → `.service` | Weekday mornings. Posts the contributor pull request digest. |

`zendesk-alert@.service` and `github-prs-alert@.service` are pulled in by `OnFailure=`
and report the failed unit to the channel that job posts to.

One clone at `/opt/zendesk` holds all of it — the directory is named after its first
tenant, not its contents. The venvs are separate, because the two jobs pin `requests`
differently and a shared one would silently be whichever was installed last.

## Host requirements

- Linux with systemd 252 or newer (the timer needs a timezone in `OnCalendar=`), and Python 3.12+
- **The Claude Code CLI installed and logged in as the service user.** Both Claude
  calls go through it — the digest's classification and the reply flow's
  translation — so its login is the only Claude credential this box holds. It must be
  installed *by* that account: the CLI keeps its binary, its login and its cache under
  `$HOME`, which is `/home/zendesk` and deliberately not the code directory. A root
  install lands in `/root/.local`, which is `0700` and unreadable to the service; see
  step 1 under Install.
- **Always on.** A workstation is not a candidate: user timers stop at logout unless
  lingering is enabled, and a sleeping laptop silently skips the digest.
- A public DNS name resolving here, with **80 and 443 reachable** — 80 for certbot's
  HTTP-01 challenge, which is what the existing nginx setup already uses.
- **nginx already installed**, with certbot managing its certificates. This adds one
  server block to it rather than a second web server; two would fight over :443 and
  take the host's other sites down with them.
- Persistent `/var/lib/zendesk` and `/var/lib/github-prs` — they hold the two digests'
  dedup state, the only thing on disk. `StateDirectory=` creates the second one.

Note who else holds root. This box becomes custodian of a Zendesk API token that can
write a public comment to any ticket, and of a logged-in Claude Code session.

## Install

Run as root. Every command below assumes it; prefix with `sudo` if you are not.

```bash
# 1. A user that owns nothing else. Its home is NOT the code directory: the Claude
#    Code CLI writes its binary, login and cache into $HOME, and /opt/zendesk is
#    mounted read-only for the relay. Both units mask /home and bind only this one
#    back in, so the account needs a home that exists before either unit starts.
#
#    nologin costs nothing here: `runuser -u` execs the command directly rather than
#    through the account's shell, so every step below still works.
useradd --system --home /home/zendesk --shell /usr/sbin/nologin zendesk
install -d -o zendesk -g zendesk -m 700 /home/zendesk

#    Install and log the CLI in AS that user. `runuser -u` resets neither HOME nor
#    PATH, so both are spelled out: without the explicit HOME the login lands in
#    root's home, and a bare `claude` resolves against root's PATH, which reports
#    `Permission denied` for a binary that is installed perfectly well.
runuser -u zendesk -- env HOME=/home/zendesk sh -c 'curl -fsSL https://claude.ai/install.sh | bash'
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude

# 2. The code and its venv
git clone https://github.com/session-foundation/session-shared-scripts /opt/zendesk
python3 -m venv /opt/zendesk/venv
/opt/zendesk/venv/bin/pip install -r /opt/zendesk/zendesk_triage/requirements.txt
chown -R zendesk:zendesk /opt/zendesk

# 3. State
install -d -o zendesk -g zendesk -m 750 /var/lib/zendesk

# 4. Secrets. Create the file with its final mode and owner *before* anything goes
#    in it: editing it into place first would leave the Zendesk token briefly
#    world-readable at the editor's default 0644. The guard makes this re-runnable
#    — install from /dev/null would otherwise truncate an existing file.
install -d -m 750 -o root -g zendesk /etc/zendesk
[ -e /etc/zendesk/env ] || install -m 640 -o root -g zendesk /dev/null /etc/zendesk/env
"${EDITOR:-nano}" /etc/zendesk/env                # contents under Secrets, below

# 5. Units
cp /opt/zendesk/deploy/*.service /opt/zendesk/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zendesk-relay.service zendesk-digest.timer

# 6. TLS, through the nginx and certbot already on this host. Point DNS at the
#    box first or certbot has nothing to validate against. The shipped config is
#    HTTP-only on purpose — certbot adds the TLS directives to that same server
#    block, so no location needs moving.
#
#    Validate before certbot, activate after. certbot reloads nginx itself while
#    it answers the challenge, so reloading beforehand would only widen the moment
#    the proxy is reachable over plain HTTP. --redirect is explicit so that once
#    TLS is in place HTTP can only redirect, never proxy an unencrypted POST.
cp /opt/zendesk/deploy/nginx-webhooks.conf /etc/nginx/sites-available/webhooks.session.codes
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
> diff /opt/zendesk/deploy/nginx-webhooks.conf \
>      /etc/nginx/sites-available/webhooks.session.codes
> "${EDITOR:-nano}" /etc/nginx/sites-available/webhooks.session.codes
> nginx -t && systemctl reload nginx
> ```

### Adding the pull request digest

Its own user, its own environment file and its own venv, out of the same clone. A
token that can read the org's repositories has no business in the environment of the
relay, which is the one process here reachable from the internet.

The account needs no home of its own: nothing in this job shells out to the Claude
CLI, so the `$HOME` that the Zendesk units bend over backwards to preserve is not
wanted here at all.

```bash
useradd --system --no-create-home --home /nonexistent --shell /usr/sbin/nologin ghdigest
python3 -m venv /opt/github-prs/venv
/opt/github-prs/venv/bin/pip install -r /opt/zendesk/github_prs/requirements.txt
chown -R ghdigest:ghdigest /opt/github-prs

install -d -m 750 -o root -g ghdigest /etc/github-prs
[ -e /etc/github-prs/env ] || install -m 640 -o root -g ghdigest /dev/null /etc/github-prs/env
"${EDITOR:-nano}" /etc/github-prs/env             # contents under Secrets, below

cp /opt/zendesk/deploy/github-prs-*.service /opt/zendesk/deploy/github-prs-*.timer \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now github-prs-digest.timer
```

The clone stays owned by `zendesk` and world-readable, which is what lets `ghdigest`
run out of it. Nothing secret lives there — every secret is under `/etc`.

`/var/lib/github-prs` needs no `install` step: `StateDirectory=github-prs` on the unit
creates it with the right owner on first start. It holds the dedup state that keeps the
72-hour window from re-reporting the same PR every weekday morning.

## Secrets

`/etc/zendesk/env`, mode `640`, `root:zendesk` — readable by the service, not by
everyone. `EnvironmentFile=` needs no code change because every script already reads
its configuration from the environment.

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
#     install -o zendesk -g zendesk -m 640 house_answers.json /var/lib/zendesk/
#ZENDESK_HOUSE_ANSWERS=/var/lib/zendesk/house_answers.json

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
systemd-run --pipe --wait --uid=zendesk -p EnvironmentFile=/etc/zendesk/env env | grep -vi token
```

Set `RELAY_DRY_RUN=1` for the first deployment. The whole `claude:` note path runs —
webhook, command parsing, composing, translation — and the Zendesk writes are
skipped.

### `/etc/github-prs/env`

Mode `640`, `root:ghdigest`, and separate from the Zendesk file rather than merged
into it — see above.

```sh
# Read-only, and no scope at all: the digest reads public repositories only. Nothing
# here ever writes to GitHub.
GITHUB_PRS_TOKEN=

# The channel the digest posts to. A webhook is bound to the channel it was created
# in, so this one value decides where the digest goes.
GITHUB_PRS_DISCORD_WEBHOOK_URL=

# Required, and normally the same webhook: without it alert.py falls back to
# ZENDESK_DISCORD_WEBHOOK_URL, which is not in this file, and the failure notifier
# fails instead of reporting.
ALERT_DISCORD_WEBHOOK_URL=
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
whether an edit to `/etc/zendesk/env` has reached it yet — it does not until the unit
is restarted.

Both the explicit `HOME` and the absolute path are load-bearing: `runuser -u` resets
neither, so a bare `claude` there is resolved against root's `PATH` and reports
`Permission denied` for an install that is fine.

With `CLAUDE_CODE_OAUTH_TOKEN` set, that check tests the wrong credential — it reads
the interactive login, which the token outranks. Test what the unit will actually use:

```bash
systemd-run --pty --uid=zendesk -p EnvironmentFile=/etc/zendesk/env \
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

**2. The digest, by hand.** `systemctl start zendesk-digest.service` and watch
`journalctl -fu zendesk-digest`. It prints ticket counts and outcomes, never content.

**3. The timer fires when you expect.** `systemctl list-timers zendesk-digest` — and
if you change `OnCalendar=`, check it with
`systemd-analyze calendar "Mon..Fri 10:00 Australia/Melbourne"`. There is no
`Timezone=` key and systemd ignores one silently.

**4. Through Zendesk.** With the webhook and the trigger in place (see the main
[README](../README.md)), write `claude: english` as a private note on a throwaway
ticket and watch `journalctl -fu zendesk-relay`. `english` is the read-only verb, so
nothing can reach a customer if the wiring is wrong.

The ticket needs at least one **public** comment, ideally not in English. `english`
translates the conversation, and a ticket carrying only private notes has nothing to
work on — it answers "there are no public comments on this ticket to translate",
which proves the wiring but exercises none of the model path.

The one thing only this step can prove is that the relay's HMAC matches Zendesk's. If
it does not, every note logs a 401 and nothing happens.

**5. The failure path.** `systemctl start zendesk-alert@test.service` should put
a line in the triage channel.

The alert quotes the failed unit's last journal line, which is what tells the channel
whether the job broke or the Claude Code login simply expired — the two look identical
otherwise, and only one of them is fixed by re-running anything. That needs
`SupplementaryGroups=systemd-journal` on the alert unit, so check it reached systemd
and that the account can actually read a journal:

```bash
systemctl show zendesk-alert@test.service -p SupplementaryGroups
runuser -u zendesk -- journalctl -u zendesk-digest.service -n 1 --no-pager
```

An empty `SupplementaryGroups=` means the installed unit is the old copy — see
Updating. A permission error from the second command costs the excerpt and nothing
else: the alert still sends, with the message it always sent.

```sh
cd deploy && python -m unittest discover     # the alert's own tests
```
**6. The pull request digest.** A dry run under the unit's own confinement renders the
digest and posts nothing. `systemd-run` rather than `runuser` because the token then
comes from the environment file rather than an argument every process on the box can
read out of `ps`:

```bash
systemd-run --pty --uid=ghdigest -p EnvironmentFile=/etc/github-prs/env \
  -p WorkingDirectory=/opt/zendesk/github_prs \
  /opt/github-prs/venv/bin/python digest.py --dry-run
```

Then for real: `systemctl start github-prs-digest.service`,
`systemctl list-timers github-prs-digest` (expect the next weekday, not tomorrow), and
`systemctl start github-prs-alert@test.service` for its failure path.

Start it twice. The second run is the one that proves the dedup state: it should report
everything, then nothing, and say so.

```bash
journalctl -u github-prs-digest -n 5 --no-pager   # "N new, 0 changed, N unchanged"
ls -l /var/lib/github-prs/seen.json
```

A second run that reports everything again means the state was not written — check that
`StateDirectory=` reached systemd with
`systemctl show github-prs-digest -p StateDirectory`.

## Updating

```bash
runuser -u zendesk -- git -C /opt/zendesk pull
/opt/zendesk/venv/bin/pip install -r /opt/zendesk/zendesk_triage/requirements.txt
/opt/github-prs/venv/bin/pip install -r /opt/zendesk/github_prs/requirements.txt
systemctl restart zendesk-relay
```

The units live in `/etc/systemd/system`, so a pull that changes anything under
`deploy/` needs them copied again — systemd reads the installed copy, not the
checkout, and a stale unit fails in whatever way the old one did:

```bash
git -C /opt/zendesk diff --stat HEAD@{1} HEAD -- deploy/    # did any unit change?
cp /opt/zendesk/deploy/*.service /opt/zendesk/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart zendesk-relay
```

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
"${EDITOR:-nano}" /etc/zendesk/env   # ZENDESK_DISCORD_WEBHOOK_URL,
                                     # ZENDESK_WEBHOOK_SECRET, and RELAY_DRY_RUN=1
                                     # for the first run
mv /var/lib/zendesk/seen.json /var/lib/zendesk/seen.json.old
systemctl restart zendesk-relay
systemctl start zendesk-digest.service
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

Failures that are not a whole-host outage report themselves — `OnFailure=` on both
units posts the failed unit and a `journalctl` line to the triage channel.