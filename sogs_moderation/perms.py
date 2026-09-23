#!/usr/bin/env python3
"""
Grant or revoke one account's permissions in a room we moderate.

Written for testing [ban.py](ban.py): our rooms are read-only to everyone but
moderators, so an account that is going to be banned and have its posts deleted needs
write permission first, or the deletion step has nothing to act on and proves nothing.

It is also the way to mute one account in a room (`--write off`) without banning it.

    python perms.py --room session-updates --write on --upload on 05<test account>
    python perms.py --room session-updates --write default --upload default 05<id>

`on` grants, `off` denies, `default` removes the override and returns the account to
the room's own default. A room with `default_write` false is read-only, so `off` and
`default` differ only in what a later change to the room default would do.

The server and the moderator key come from ban.py: same SOGS, same SOGS_MOD_SEED.

## Which id form, and why the account has to exist first

POST /room/<token>/permissions/ takes a blinded (15) id on our server — the 05 form
404s on the route itself (see "Which id form" in ban.py). Blinding loses the key's
sign, so a 05 id yields two possible blinded ids and only one of them is the account.

Guessing wrong is silent rather than loud: the endpoint creates the account row it was
given, so the permission would be granted to an id nobody holds, and the answer would
look exactly like success. So the two candidates are resolved first, against
POST /inbox/<id> with an empty body — 400 (no message provided) is the server saying
that account exists, 404 that it does not. The probe stores nothing: the 400 comes
before the message is read.

An account the server has never seen answers 404 under both, which is what happens
when the test account has not opened the community yet. A banned account answers 404
under both as well, so unban it before granting.
"""

import argparse
import sys

from ban import (
    SESSION_ID_RE,
    SOGS_PUBKEY,
    SOGS_URL,
    Sogs,
    SogsError,
    blinded_ids,
    capabilities,
    ed25519_pubkey,
    read_moderator_key,
    session_id_of,
)

PERMISSIONS = ('read', 'write', 'upload', 'accessible')


def permission_body(args):
    """The endpoint's body: a permission set true/false, or its default_ flag to clear it."""
    body = {}
    for name in PERMISSIONS:
        value = getattr(args, name)
        if value == 'default':
            body['default_' + name] = True
        elif value is not None:
            body[name] = value == 'on'
    return body


def resolve_blinded_id(client, sid):
    """The blinded id this account is stored under, of the two its 05 id could blind to."""
    found = []
    for candidate in blinded_ids(sid, client.server_pubkey):
        code, body = client.request('POST', f'/inbox/{candidate}', {})
        if code == 400:
            found.append(candidate)
        elif code != 404:
            raise SogsError(f"Probing {candidate} answered {code}: {body}")

    if not found:
        raise SogsError(
            f"{sid} is not an account this server can act on. It has either never "
            "opened one of our communities, or it is banned — unban it first."
        )
    if len(found) > 1:
        raise SogsError(f"{sid} matched both blinded ids, which should not happen: {found}")
    return found[0]


def main():
    ap = argparse.ArgumentParser(
        description=f"Set one account's room permissions on {SOGS_URL}.",
        epilog="SOGS_MOD_SEED must be the seed of a moderator of the given room(s).",
    )
    ap.add_argument('session_id', metavar='SESSION_ID', help="05... Session ID to act on")
    ap.add_argument('--room', metavar='TOKEN', action='append', required=True,
                    help="Room token; repeat for several rooms")
    for name in PERMISSIONS:
        ap.add_argument(f'--{name}', choices=('on', 'off', 'default'),
                        help=f"Grant, deny, or restore the room default for {name}")
    ap.add_argument('--dry-run', action='store_true', help="Print what would be sent and exit")
    ap.add_argument('--yes', '-y', action='store_true', help="Do not ask for confirmation")
    args = ap.parse_args()

    sid = args.session_id.lower()
    if not SESSION_ID_RE.match(sid):
        raise SogsError(f"Not a 05 Session ID: {args.session_id}")
    ed25519_pubkey(sid)

    body = permission_body(args)
    if not body:
        raise SogsError(f"Nothing to change: pass at least one of {', '.join('--' + p for p in PERMISSIONS)}")

    signing_key = read_moderator_key()
    server_pubkey = bytes.fromhex(SOGS_PUBKEY)
    client = Sogs(SOGS_URL, server_pubkey, signing_key, 'blind' in capabilities(SOGS_URL))

    code, rooms = client.get('/rooms')
    if code != 200:
        raise SogsError(f"GET /rooms returned {code}: {rooms}")
    known = {r['token']: r for r in rooms}
    for token in args.room:
        if token not in known:
            raise SogsError(f"No room {token} on {SOGS_URL}; visible rooms: {', '.join(sorted(known))}")
        if not (known[token].get('moderator') or known[token].get('global_moderator')):
            raise SogsError(f"{session_id_of(signing_key)} is not a moderator of {token}")

    changes = ', '.join(f"{k}={v}" for k, v in body.items())
    print(f"Setting {changes} for {sid}\n  in {', '.join(args.room)} on {SOGS_URL}")

    if args.dry_run:
        print("\nDry run: nothing was sent.")
        return 0
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ('y', 'yes'):
        print("Aborted.")
        return 1

    blinded = resolve_blinded_id(client, sid)
    print(f"\nblinded id    {blinded}")

    failures = []
    for token in args.room:
        code, res = client.request('POST', f'/room/{token}/permissions/{blinded}', body)
        # The endpoint answers with the account's remaining overrides, so an empty
        # object is the confirmation that the last one is gone.
        if isinstance(res, dict):
            note = ', '.join(f"{k}: {v}" for k, v in res.items()) or 'no overrides left'
        else:
            note = str(res)[:120]
        print(f"  {token:<18}{code}  {note}")
        if not 200 <= code < 300:
            failures.append(f"{token}: {code} {str(res)[:200]}")

    for f in failures:
        print(f"\nFAILED: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SogsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
