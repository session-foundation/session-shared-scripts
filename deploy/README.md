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
- **Always on.** A workstation is not a candidate: user timers stop at logout unless
  lingering is enabled, and a sleeping laptop silently skips the digest.
- A public DNS name resolving here, with **80 and 443 reachable** — 80 for certbot's
  HTTP-01 challenge, which is what the existing nginx setup already uses.
- **nginx already installed**, with certbot managing its certificates. This adds one
  server block to it rather than a second web server; two would fight over :443 and
  take the host's other sites down with them.
- Persistent `/var/lib/zendesk` — it holds the dedup state, the only thing on disk.

Note who else holds root. This box becomes custodian of a Zendesk API token that can
write a public comment to any ticket, and of the Anthropic key.

## Install

```bash
# 1. A user that owns nothing else
sudo useradd --system --home /opt/zendesk --shell /usr/sbin/nologin zendesk

# 2. The code and its venv
sudo git clone https://github.com/session-foundation/session-shared-scripts /opt/zendesk
sudo python3 -m venv /opt/zendesk/venv
sudo /opt/zendesk/venv/bin/pip install -r /opt/zendesk/zendesk_triage/requirements.txt
sudo chown -R zendesk:zendesk /opt/zendesk

# 3. State
sudo install -d -o zendesk -g zendesk -m 750 /var/lib/zendesk

# 4. Secrets — see below
sudo install -d -m 750 /etc/zendesk
sudo -e /etc/zendesk/env
sudo chown root:zendesk /etc/zendesk/env && sudo chmod 640 /etc/zendesk/env

# 5. Units
sudo cp /opt/zendesk/deploy/*.service /opt/zendesk/deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zendesk-relay.service zendesk-digest.timer

# 6. TLS, through the nginx and certbot already on this host. Point DNS at the
#    box first or certbot has nothing to validate against. The shipped config is
#    HTTP-only on purpose — certbot adds the TLS directives to that same server
#    block, so no location needs moving.
#
#    Validate before certbot, activate after. certbot reloads nginx itself while
#    it answers the challenge, so reloading beforehand would only widen the moment
#    the proxy is reachable over plain HTTP. --redirect is explicit so that once
#    TLS is in place HTTP can only redirect, never proxy an unencrypted POST.
sudo cp /opt/zendesk/deploy/nginx-webhooks.conf /etc/nginx/sites-available/webhooks.session.codes
sudo ln -s /etc/nginx/sites-available/webhooks.session.codes /etc/nginx/sites-enabled/
sudo nginx -t                                     # validate; do not reload yet
sudo certbot --nginx --redirect -d webhooks.session.codes
sudo nginx -t && sudo systemctl reload nginx      # validate what certbot wrote
```

## Secrets

`/etc/zendesk/env`, mode `640`, `root:zendesk` — readable by the service, not by
everyone. `EnvironmentFile=` needs no code change because every script already reads
its configuration from the environment.

```sh
ZENDESK_SUBDOMAIN=
ZENDESK_EMAIL=                    # authors every comment the reply flow posts
ZENDESK_API_TOKEN=
ANTHROPIC_API_KEY=
DISCORD_BOT_TOKEN=                # the digest posts as the app, for its buttons
ZENDESK_DISCORD_CHANNEL_ID=       # where the digest lands
ZENDESK_DISCORD_WEBHOOK_URL=      # the review tally and the failure alerts
DISCORD_PUBLIC_KEY=               # verifies Discord's request signatures
DISCORD_GUILD_ID=                 # interactions from anywhere else are refused
ALLOWED_USER_IDS=                 # comma-separated; either list grants,
ALLOWED_ROLE_IDS=                 # and both empty refuses everybody
# RELAY_DRY_RUN=1                 # run the whole path, write nothing to Zendesk
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

**2. The digest, by hand.** `sudo systemctl start zendesk-digest.service` and watch
`journalctl -fu zendesk-digest`. It prints ticket counts and outcomes, never content.

**3. The timer fires when you expect.** `systemctl list-timers zendesk-digest` — and
if you change `OnCalendar=`, check it with
`systemd-analyze calendar "Mon..Fri 10:00 Australia/Brisbane"`. There is no
`Timezone=` key and systemd ignores one silently.

**4. Through Discord.** Point the app's **Interactions Endpoint URL** at
`https://webhooks.session.codes/discord/interactions`. Discord sends its own signed
`PING` and refuses the
URL if verification is wrong, so saving it *is* the smoke test. Then press **Comment**
on a digest card for a throwaway ticket whose requester is an address you own — the
card's button is the only way in, so this is also the check that the digest and the
relay agree on the `comment:` prefix.

**5. The failure path.** `sudo systemctl start zendesk-alert@test.service` should put
a line in the triage channel.

## Updating

```bash
sudo -u zendesk git -C /opt/zendesk pull
sudo /opt/zendesk/venv/bin/pip install -r /opt/zendesk/zendesk_triage/requirements.txt
sudo systemctl restart zendesk-relay
```

Manual on purpose. Automating this would mean giving CI an SSH key to the box, which
is the coupling self-hosting was meant to remove.

## If this host goes down

Nothing else runs any of this — the scheduled jobs and the interactions endpoint both
live here only, so recovery means fixing the host rather than failing over.

What that costs, in order of how much it matters:

- **Replies stop.** A Comment button on a digest card gets no answer at all. Nothing
  is half-written: `reply.py` posts the public comment before the private note, and
  the marker in that note means re-running the same interaction cannot double-post.
- **The digest is late, not lost.** `Persistent=yes` on the timer means a host that
  was down at 10:00 runs the digest once when it comes back, and the 72-hour window
  covers the gap.
- **The dedup state may be stale.** Losing `/var/lib/zendesk/seen.json` re-reports the
  window once: noisy, never wrong.

Failures that are not a whole-host outage report themselves — `OnFailure=` on both
units posts the failed unit and a `journalctl` line to the triage channel.