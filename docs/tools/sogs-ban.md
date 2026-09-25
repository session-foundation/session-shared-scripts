# Community Bans

Abuse reports arrive through Zendesk with a Session ID. [`ban.py`](../../src/session_ops/sogs/ban.py)
bans those IDs from the whole SOGS we run, not room by room, and prints what the
server answered at each step, so the reply to the ticket can say what happened:

| | |
| --- | --- |
| Runs | by hand, from a checkout; nothing schedules it |
| Secrets | `SOGS_MOD_SEED`, a moderator's key, in a local `.env` |
| Dry run | `uv run sogs-ban --dry-run 05…` |

```sh
set -a && . ./.env && set +a                 # SOGS_MOD_SEED
uv run sogs-ban 05abc...def
```

The ban is server-wide, and the deletion follows once it has landed. The order matters:
a globally banned account cannot make any further request, so it cannot post into a room
the deletion has already walked.

| flag | |
| --- | --- |
| `--from-file PATH` | one ID per line, `#` comments allowed |
| `--unban` | lift the ban; deleted messages are gone for good |
| `--dry-run` | print the steps, send no ban or deletion |
| `--yes` / `-y` | skip the confirmation prompt |
| `--whoami` | print the Session ID of the configured key |

There is no flag to keep the messages or to narrow the scope: the tool exists for abuse
reports, where both are always wanted.

## Confirming the ban

Each step's HTTP status is the confirmation, and the script prints one line per step:

```
05abc...def
  server-wide ban         200
  delete messages (15)    200  17 deleted (session-updates: 12, oxen-updates: 5)
```

pysogs does the work inside the request — `user.ban()` writes the row before the
handler returns — so a `2xx` is the server saying it applied that step. A `/sequence`
stops at its first failure, so a partial application shows as a short reply: the steps
that ran are printed, the id is counted as failed, and the run exits non-zero.

A refused step prints the server's answer rather than a count, so a deletion that was
turned down cannot read as an emptied account in the line that answers the ticket.

A deletion that never reaches the server prints `error` in place of a status, so the ban
that already landed is still reported. Re-run to retry it: the ban is idempotent, and an
errored attempt leaves the remaining id forms untried.

There is deliberately no read-back of the resulting state, because nothing on the server
lists globally banned accounts: `GET /room/<token>/permissions` reports room-level bans
only, and answers `500` on ours anyway. An inbox probe distinguishes a globally banned
account from a live one, but only as a before/after pair — on its own, a `404` can't be
told apart from an account the server has never seen.

## Which id form

Our server is older than pysogs [`21e2ef2`](https://github.com/session-foundation/session-pysogs/commit/21e2ef2),
which widened several routes from blinded ids to either form, and it runs with
`REQUIRE_BLIND_KEYS` — both conditions are needed to see this. Probed against it:

| route | `05…` | `15…` |
| ----- | ----- | ----- |
| `/user/<id>/ban` | ok | ok |
| `/rooms/all/<id>` | `404` | ok |
| `/room/<token>/permissions/<id>` | `404` | ok |

So a ban takes the 05 id straight from the ticket and the server resolves it to the
blinded account itself, while deleting messages has to go under the blinded id. The
catch is that `404` is also what those routes answer for an account they have never
seen, so a deletion that only tried the 05 form would report an emptied account for
every id. Blinding loses the key's sign, which leaves two possible blinded ids and no
way to derive which is real, so the deletion tries each in turn and the first non-`404`
answers. The line reports which form it went under:

```
  delete messages (15)    200  17 deleted (session-updates: 12, oxen-updates: 5)
```

Once the server is upgraded past `21e2ef2` the fallback can be dropped: those routes
then resolve a 05 id themselves, and against both blinded candidates, so the first
attempt answers and the loop never reaches the rest. It is harmless until then.

## Letting a test account post

Our rooms are read-only to everyone but moderators, so a test account has nothing for
the deletion step to delete and the run proves nothing.
[`perms.py`](../../src/session_ops/sogs/perms.py) grants it write permission in
one room, and takes it back afterwards:

```sh
uv run sogs-perms --room session-updates --write on --upload on 05<test account>
# ... post from that account, then ban it, then:
uv run sogs-perms --room session-updates --write default --upload default 05<test account>
```

`on` grants, `off` denies (muting one account without banning it), `default` drops the
override back to the room's own default. The endpoint answers with the account's
remaining overrides, so an empty object is the confirmation that the last one is gone.

This route is blinded-id-only too, and here guessing the wrong one of the two is
silent rather than loud: the endpoint creates the account row it is given, so the
permission would land on an id nobody holds and the answer would look like success.
The two candidates are resolved first against `POST /inbox/<id>` with an empty body —
`400` is the server saying that account exists, `404` that it does not, and nothing is
delivered either way because the `400` comes before the message is read. An account
that has never opened the community answers `404` under both, and so does a banned
one: unban before granting.

## The server

`SOGS_URL` and `SOGS_PUBKEY` are constants in the script, not configuration. This bans
people from the communities we run, and the only thing a flag that retargets it can
add is a bulk ban on somebody else's server. Point it elsewhere by editing those two
lines, deliberately.

## The moderator key

`SOGS_MOD_SEED` is the Ed25519 seed the requests are signed with, and whoever holds it
is a global moderator of the server. It lives in the repo's gitignored `.env`, sourced
into the environment like the Zendesk jobs' secrets. The account must already be a
global moderator or admin — `--whoami` prints its Session ID, and on the server:

```sh
python3 -msogs --add-moderators 05<id> --rooms + --hidden
```

For the bans themselves, blinded IDs need no handling: the server maps the 05 ID to
the blinded account, and records the ban for later if that account has not connected
yet. Deleting messages is the step that needs the blinded form — see
[Which id form](#which-id-form).

## Tests

```sh
sudo apt install python3-session-util      # see "Dependencies" below
uv venv --system-site-packages --python /usr/bin/python3 && uv sync
uv run python -m unittest discover -s tests/sogs -t . -v
```

The blinded signature is checked by verifying it under the blinded pubkey rather than
against a fixed vector. A blinded signature is not deterministic across implementations,
because the nonce derivation is not part of what a verifier checks, and pysogs accepts it
as a plain Ed25519 signature under that pubkey. The unblinded signature is deterministic
and is still checked against pysogs' published vector.

## Dependencies

The blinded request signatures come from `session_util`, libsession-util's Python
binding. It is published as a deb rather than a wheel, so `pip` cannot reach it:

```sh
# https://deb.oxen.io has the repository setup
sudo apt install python3-session-util
```

It is a compiled extension built per Python minor version, so a Python upgrade needs a
matching package, and a virtualenv needs `--system-site-packages` to see it. This is why
`sogs-ban` is run from a checkout by hand rather than deployed anywhere.

pynacl stays for the blinding factor and for `blinded_ids`, which the deletion step walks:
a Session ID does not carry the sign of the key behind it, so both candidates have to be
tried. `session_util` grew a `blind15_id` covering this in
[`7e8d126`](https://github.com/session-foundation/libsession-python/commit/7e8d126), which
no packaged release carries yet.
