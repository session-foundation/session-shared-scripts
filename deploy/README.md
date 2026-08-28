# Self-hosted deployment

The Zendesk triage digest and the reply-from-Discord flow, both running on one
machine.

Two things run here:

| Unit | What it is |
| --- | --- |
| `zendesk-relay.service` | Always on. The HTTPS endpoint Discord posts interactions to. |
| `zendesk-digest.timer` → `.service` | Weekday mornings. Resolves positive reviews, then posts the digest. |

`zendesk-alert@.service` is pulled in by `OnFailure=` on both, and reports the failed
unit to the triage channel.

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
- Persistent `/var/lib/zendesk` — it holds the dedup state, the only thing on disk.

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

ZENDESK_SUBDOMAIN=
# Authors every comment the reply flow posts.
ZENDESK_EMAIL=
ZENDESK_API_TOKEN=

# The digest posts as the app, because its Comment buttons need an application.
DISCORD_BOT_TOKEN=
# Where the digest lands. The real triage channel, not a test server.
ZENDESK_DISCORD_CHANNEL_ID=
# The review tally and the failure alerts. Same channel, addressed as a webhook.
ZENDESK_DISCORD_WEBHOOK_URL=

# Verifies Discord's request signatures.
DISCORD_PUBLIC_KEY=
# Interactions from any other server are refused — so this must name the same
# server as the channel and webhook above.
DISCORD_GUILD_ID=
# Comma-separated. Either list grants; both empty refuses everybody.
ALLOWED_USER_IDS=
ALLOWED_ROLE_IDS=

# Optional, and read by both units. The numeric id of a multi-line text ticket
# field in Zendesk; the digest renders every non-English ticket it is about to post
# into English there, and the compose dialog shows that instead of the original.
# Until this is set, neither half does anything and the dialog shows the customer's
# own words. Settings -> Ticket Fields -> Multi-line text, then read the id off the
# field's URL.
#ZENDESK_ENGLISH_FIELD_ID=

# Uncomment to run the whole path and write nothing to Zendesk.
#RELAY_DRY_RUN=1
```

Check what systemd actually loaded, rather than what you think you wrote:

```bash
systemctl show zendesk-digest.service -p Environment | tr ' ' '\n' | grep -vi token
```

Set `RELAY_DRY_RUN=1` for the first deployment. Everything works — dialog,
translation, preview, Send — and the two Zendesk writes are skipped.

## Verifying, in order

**1. Locally, before Discord knows the address.** An unsigned request must be refused:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  127.0.0.1:8080/discord/interactions \
  -H 'Content-Type: application/json' -d '{"type":1}'      # expect 401
curl -sS 127.0.0.1:8080/healthz                            # expect {"ok":true}
```

**1b. The Claude CLI, as the service user.** Both Claude calls shell out to it, and
this is the step most likely to be wrong after a fresh install — a login that landed
in the wrong `$HOME`, or a binary the units cannot reach, fails only when the digest
next runs.

```bash
runuser -u zendesk -- env HOME=/home/zendesk /home/zendesk/.local/bin/claude --version
systemctl show zendesk-relay -p Environment | tr ' ' '\n' | grep -E 'HOME|PATH'
```

Both the explicit `HOME` and the absolute path are load-bearing: `runuser -u` resets
neither, so a bare `claude` there is resolved against root's `PATH` and reports
`Permission denied` for an install that is fine.

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

**4. Through Discord.** Point the app's **Interactions Endpoint URL** at
`https://webhooks.session.codes/discord/interactions`. Discord sends its own signed
`PING` and refuses the
URL if verification is wrong, so saving it *is* the smoke test. Then press **Comment**
on a digest card for a throwaway ticket whose requester is an address you own — the
card's button is the only way in, so this is also the check that the digest and the
relay agree on the `comment:` prefix.

**5. The failure path.** `systemctl start zendesk-alert@test.service` should put
a line in the triage channel.

## Updating

```bash
runuser -u zendesk -- git -C /opt/zendesk pull
/opt/zendesk/venv/bin/pip install -r /opt/zendesk/zendesk_triage/requirements.txt
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

## Pointing it at another Discord server

The Zendesk half does not move. What does is everything that identifies a Discord
application, server and channel — and the application is the awkward one: it owns the
interactions endpoint *and* the bot, so a server you do not administer needs an
application owned by someone who does. `DISCORD_PUBLIC_KEY` and `DISCORD_BOT_TOKEN`
change with it. DNS, nginx and the endpoint URL itself stay exactly as they are.

What the server's admins have to do:

1. **Create an application** (Developer Portal → New Application) and add a bot to it.
   You need its **Application ID**, **Public Key**, and **bot token** — the token sent
   privately, since it can post as the app.
2. **Set the Interactions Endpoint URL** to
   `https://webhooks.session.codes/discord/interactions`. Do this *after* the new
   public key is in `/etc/zendesk/env` and the relay has been restarted: Discord signs
   its own `PING` and refuses the URL unless the relay verifies it, so saving it is
   both the last step and the smoke test.
3. **Invite the bot**:
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=19456`
   — View Channel, Send Messages, Embed Links, and nothing else. No slash commands, no
   message-content intent, no reading the channel. A private target channel also needs
   View Channel and Send Messages granted to the bot's role on the channel itself.
4. **Create a webhook in that channel** (Channel Settings → Integrations → Webhooks)
   and send the URL privately. A webhook is bound to the channel it was created in, so
   the old one cannot reach the new one.
5. **Name the role** allowed to send replies, unless you allowlist individual users.

Server and channel ids need no admin: enable Developer Mode (User Settings →
Advanced), then right-click → Copy Server ID / Copy Channel ID. A role id comes from
typing `\@RoleName` into a message box and not sending it.

Then, on the host:

```bash
"${EDITOR:-nano}" /etc/zendesk/env   # DISCORD_PUBLIC_KEY, DISCORD_BOT_TOKEN,
                                     # DISCORD_GUILD_ID, ZENDESK_DISCORD_CHANNEL_ID,
                                     # ZENDESK_DISCORD_WEBHOOK_URL, ALLOWED_ROLE_IDS,
                                     # and RELAY_DRY_RUN=1 for the first press
mv /var/lib/zendesk/seen.json /var/lib/zendesk/seen.json.old
systemctl restart zendesk-relay
systemctl start zendesk-digest.service
```

`ALLOWED_USER_IDS` survives the move untouched: Discord user ids are global, role ids
are per-server.

Move `seen.json` aside or the first digest in the new channel says almost nothing —
dedup state is per ticket, not per channel, so everything already reported to the old
one stays suppressed. Moving it re-reports the window once, which is the noisy-but-
correct outcome the state file is built around.

## If this host goes down

Nothing else runs any of this — the scheduled jobs and the interactions endpoint both
live here only, so recovery means fixing the host rather than failing over.

What that costs, in order of how much it matters:

- **Replies stop.** A Comment button on a digest card gets no answer at all. The
  worst a half-run leaves is an orphan private note and no reply: `reply.py` posts
  the note first because it carries the marker, so a re-run of the same interaction
  refuses rather than emailing the customer twice.
- **The digest is late, not lost.** `Persistent=yes` on the timer means a host that
  was down at 10:00 runs the digest once when it comes back, and the 72-hour window
  covers the gap.
- **The dedup state may be stale.** Losing `/var/lib/zendesk/seen.json` re-reports the
  window once: noisy, never wrong.

Failures that are not a whole-host outage report themselves — `OnFailure=` on both
units posts the failed unit and a `journalctl` line to the triage channel.