#!/usr/bin/env python3
"""
Ban Session IDs from the communities we run, server-wide.

Takes the 05 Session IDs from a Zendesk abuse report and, for each one, applies a
*server* ban on our SOGS: the account is blocked from the whole server, not just from
one room. It reports the server's answer to each step, so the reply to the ticket can
say what actually happened.

The bans go out as one /sequence, so the second only runs if the first succeeded:

    POST /user/<id>/ban   {"global": true}   server-wide ban
    POST /user/<id>/ban   {"rooms": ["*"]}   ban in every room we moderate

The deletion follows, once the bans have landed:

    DELETE /rooms/all/<id>                   delete their posts and uploads

/rooms/all/ takes an id our server will not accept in the 05 form (see "Which id form"
below), so the deletion is tried under each form the account can be known by.

The room bans are not redundant with the global one. A global ban is invisible to other
moderators' clients, which show a user as banned only from the room bans. Skip them
with --global-only if you ever want the global ban alone.

Deletion is irreversible and can be turned off with --keep-messages.

The ban steps need no blinded-id handling: the server maps the 05 ID to the matching
15 blinded account, and if that account has not connected yet it records the ban so it
applies when they do.

## Confirmation

Each step's HTTP status is the confirmation. pysogs does the work inside the request
— `user.ban()` writes the row before the handler returns — so a 2xx per step is the
server saying it applied that step, and the deletion step answers with its own
per-room counts.

There is deliberately no read-back. The only endpoint that reports room bans,
GET /room/<token>/permissions, answers 500 on our server, and its per-id variant
matches a blinded id only — so the statuses are the confirmation rather than a
fallback for one. An inbox probe can tell a globally banned account from a live one, but only as a
before/after pair: alone, a 404 cannot be told from an account never seen.

A /sequence stops at its first failure, so a partial application is visible rather than
silent — the steps that ran are printed and the id is counted as failed.

## Which id form

Our server is older than pysogs 21e2ef2, which widened several routes from blinded ids
to either form. Checked against it:

    /user/<id>/ban            05 ok    15 ok
    /rooms/all/<id>           05 404   15 ok
    /room/<t>/permissions/    05 404   15 ok

So the ban steps take the 05 id straight from the ticket — the server resolves it to
the blinded account itself — while the deletion has to go under the blinded id. 404 is
the same answer the route gives for an account it has never seen, so a deletion that
only tried the 05 form would report an emptied account for every id.

Which of the two blinded ids an account is stored under is not derivable (the sign is
lost in blinding), so the deletion tries each in turn and the first non-404 answers.

## Configuration

The server is SOGS_URL/SOGS_PUBKEY below, fixed in the script. This bans people from
the communities we run, so there is deliberately no flag that retargets it: a wrong
value there is a bulk ban on someone else's server.

    SOGS_MOD_SEED     Ed25519 seed (64 hex chars) of an account that is a *global*
                      moderator or admin of the server. Read from the environment,
                      which is where sourcing the repo's .env puts it:

                          set -a && . ./.env && set +a

The seed is the only secret: it is the key the requests are signed with, and anyone
holding it is a global moderator of our server.

If the key is not yet a global moderator, `--whoami` prints its Session ID, and whoever
runs the server registers it with:

    python3 -msogs --add-moderators 05<id> --rooms + --hidden

## Usage

    python ban.py 05abc...def                     # one ID
    python ban.py 05abc...def 05123...456         # several
    python ban.py --from-file ids.txt             # one ID per line, # comments allowed
    python ban.py --dry-run 05abc...def           # print what would be sent, send nothing
    python ban.py --unban 05abc...def             # undo (does not restore messages)
"""

import argparse
import json
import os
import re
import secrets
import sys
import time
from base64 import b64encode
from hashlib import blake2b, sha512

import nacl.bindings as sodium
import requests
from nacl.signing import SigningKey

SESSION_ID_RE = re.compile(r'\A05[0-9a-fA-F]{64}\Z')

# The server we moderate. Not configurable on purpose — see "Configuration" above.
SOGS_URL = 'https://open.getsession.org'
SOGS_PUBKEY = 'a03c383cf63c3c4efe67acc52112a6dd734b3a946b9545f488aaa93da7991238'

# Curve25519 field order, for the u -> y birational map in ed25519_pubkey().
FIELD_P = 2**255 - 19

HTTP_TIMEOUT = 30



class SogsError(RuntimeError):
    pass


class PartialBan(SogsError):
    """A /sequence that stopped part-way. Carries the steps that did run.

    Distinct from a plain SogsError because the account is left half-actioned: the
    caller has to print what landed before it gives up on this id.
    """

    def __init__(self, sid, outcome, total_steps):
        self.sid, self.outcome, self.total_steps = sid, outcome, total_steps
        ran = len(outcome)
        label, code, body = outcome[-1] if outcome else ('?', '?', '')
        super().__init__(f"{sid}: stopped at step {ran}/{total_steps} "
                         f"({label} -> {code}: {str(body)[:200]})")


def sha512_parts(*parts):
    hasher = sha512()
    for part in parts:
        if isinstance(part, (list, tuple)):
            for p in part:
                hasher.update(p)
        else:
            hasher.update(part)
    return hasher.digest()


def blinding_factor(server_pubkey: bytes) -> bytes:
    return sodium.crypto_core_ed25519_scalar_reduce(blake2b(server_pubkey, digest_size=64).digest())


def blinded_keys(server_pubkey: bytes, signing_key: SigningKey):
    """Returns (ka, kA): our blinded private scalar and blinded pubkey for this server."""
    k = blinding_factor(server_pubkey)
    # to_curve25519_private_key() is the sodium-supported way to get 'a', the Ed25519
    # private scalar: the converted X25519 key is that same scalar.
    a = signing_key.to_curve25519_private_key().encode()
    ka = sodium.crypto_core_ed25519_scalar_mul(k, a)
    return ka, sodium.crypto_scalarmult_ed25519_base_noclamp(ka)


def blinded_signature(message_parts, signing_key: SigningKey, ka: bytes, kA: bytes) -> bytes:
    """
    Ed25519 signature under the blinded key, with kA mixed into the hash that yields r
    so that different blinded pubkeys are domain separated. Verification is unaffected.
    """
    h_rh = sha512(signing_key.encode()).digest()[32:]
    r = sodium.crypto_core_ed25519_scalar_reduce(sha512_parts(h_rh, kA, message_parts))
    sig_r = sodium.crypto_scalarmult_ed25519_base_noclamp(r)
    hram = sodium.crypto_core_ed25519_scalar_reduce(sha512_parts(sig_r, kA, message_parts))
    sig_s = sodium.crypto_core_ed25519_scalar_add(r, sodium.crypto_core_ed25519_scalar_mul(hram, ka))
    return sig_r + sig_s


def auth_headers(signing_key, server_pubkey, method, path, timestamp, nonce, body, blinded):
    """
    The X-SOGS-* headers for one request. The signature covers

        SERVER_PUBKEY || NONCE || TIMESTAMP || METHOD || PATH || HBODY

    where HBODY is the 64-byte blake2b hash of the body, omitted when there is none.
    """
    to_sign = [server_pubkey, nonce, str(timestamp).encode(), method.encode(), path.encode()]
    if body:
        to_sign.append(blake2b(body, digest_size=64).digest())

    if blinded:
        ka, kA = blinded_keys(server_pubkey, signing_key)
        pubkey = '15' + kA.hex()
        sig = blinded_signature(to_sign, signing_key, ka, kA)
    else:
        pubkey = '00' + signing_key.verify_key.encode().hex()
        sig = signing_key.sign(b''.join(to_sign)).signature

    return {
        'X-SOGS-Pubkey': pubkey,
        'X-SOGS-Timestamp': str(timestamp),
        'X-SOGS-Nonce': b64encode(nonce).decode(),
        'X-SOGS-Signature': b64encode(sig).decode(),
    }


def session_id_of(signing_key: SigningKey) -> str:
    return '05' + signing_key.to_curve25519_private_key().public_key.encode().hex()


def ed25519_pubkey(session_id: str) -> bytes:
    """
    The Ed25519 point behind a 05 Session ID, whose hex is an X25519 pubkey.

    Rejects hex that is the right shape but not a real key: the scalar multiplication
    in blinded_ids() fails on a point that is not on the curve, and a Session ID
    mistyped out of a ticket is exactly how that happens.
    """
    u = int.from_bytes(bytes.fromhex(session_id[2:]), 'little')
    y = ((u - 1) * pow(u + 1, FIELD_P - 2, FIELD_P) % FIELD_P).to_bytes(32, 'little')
    if not sodium.crypto_core_ed25519_is_valid_point(y):
        raise SogsError(f"{session_id} is not a usable Session ID: not a valid public key")
    return y


def blinded_ids(session_id: str, server_pubkey: bytes):
    """
    The two blinded IDs a 05 Session ID can appear under on this server.

    Blinding maps the account's X25519 pubkey back to an Ed25519 point, which recovers
    the key only up to its sign, so the account may be stored under either variant and
    both have to be checked.
    """
    kA = sodium.crypto_scalarmult_ed25519_noclamp(
        blinding_factor(server_pubkey), ed25519_pubkey(session_id)
    )
    return (
        '15' + (kA[:31] + bytes([kA[31] & 0x7F])).hex(),
        '15' + (kA[:31] + bytes([kA[31] | 0x80])).hex(),
    )


class Sogs:
    def __init__(self, base_url, server_pubkey, signing_key, blinded):
        self.base_url = base_url.rstrip('/')
        self.server_pubkey = server_pubkey
        self.signing_key = signing_key
        self.blinded = blinded
        self.http = requests.Session()

    def request(self, method, path, body_json=None):
        body = None
        headers = {}
        if body_json is not None:
            # The signature is over the exact bytes sent, so serialize once and post that.
            body = json.dumps(body_json, separators=(',', ':')).encode()
            headers['Content-Type'] = 'application/json'

        headers.update(
            auth_headers(
                self.signing_key,
                self.server_pubkey,
                method,
                path,
                int(time.time()),
                secrets.token_bytes(16),
                body,
                self.blinded,
            )
        )

        try:
            r = self.http.request(
                method, self.base_url + path, data=body, headers=headers, timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as e:
            raise SogsError(f"{method} {path} failed: {e}") from e

        if r.headers.get('content-type', '').startswith('application/json'):
            return r.status_code, r.json()
        return r.status_code, r.text

    def get(self, path):
        return self.request('GET', path)

    def sequence(self, subrequests):
        """
        POST /sequence: runs subrequests in order, stopping at the first failure.

        Returns one {code, body} per request that ran, so the reply is shorter than the
        request when a step fails — that shortfall is how a partial application is
        detected. Sent whole rather than chunked, because chunking would let the steps
        after a failure run in the next chunk.
        """
        code, body = self.request('POST', '/sequence', subrequests)
        if code != 200:
            raise SogsError(f"POST /sequence returned {code}: {body}")
        return body


def capabilities(base_url):
    try:
        r = requests.get(base_url.rstrip('/') + '/capabilities', timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return set(r.json().get('capabilities', ()))
    except (requests.RequestException, ValueError) as e:
        raise SogsError(f"Could not read {base_url}/capabilities: {e}") from e


def apply_to(client, sid, unban, ban_rooms, delete_messages):
    """Applies the ban (or unban) to one Session ID.

    Returns [(label, status code, body)] in the order the steps ran, which is what the
    run is confirmed by: the server does the work inside the request, so a 2xx per step
    is the server saying it did it. A /sequence stops at its first failure, so a short
    list is a partial application and is reported as one.
    """
    verb = 'unban' if unban else 'ban'
    steps = [('server-wide ' + verb, {'method': 'POST', 'path': f'/user/{sid}/{verb}', 'json': {'global': True}})]
    if ban_rooms:
        steps.append((verb + ' in all rooms', {'method': 'POST', 'path': f'/user/{sid}/{verb}', 'json': {'rooms': ['*']}}))

    results = client.sequence([s[1] for s in steps])

    total_steps = len(steps) + (1 if delete_messages else 0)

    outcome = []
    for (label, _), res in zip(steps, results):
        if not 200 <= res['code'] < 300:
            outcome.append((label, res['code'], res['body']))
            raise PartialBan(sid, outcome, total_steps)
        outcome.append((label, res['code'], res['body']))

    if len(outcome) < len(steps):
        raise PartialBan(sid, outcome, total_steps)

    if delete_messages:
        form, code, body = delete_all_posts(client, sid)
        # Every form answered 404: no account here under any id the server could know.
        outcome.append((f'delete messages ({form})', code, {'total': 0, 'rooms': {}} if code == 404 else body))
        if code != 404 and not 200 <= code < 300:
            raise PartialBan(sid, outcome, total_steps)

    return outcome


def delete_all_posts(client, sid):
    """Deletes an account's posts, under whichever id form the route answers to.

    Returns (id form, status, body) of the attempt that answered. See "Which id form"
    above: on our server the 05 form 404s on the route itself, which the response
    cannot be told apart from an account the server has never seen.
    """
    code = body = None
    for candidate in (sid, *blinded_ids(sid, client.server_pubkey)):
        code, body = client.request('DELETE', f'/rooms/all/{candidate}')
        if code != 404:
            return candidate[:2], code, body
    return '05/15', code, body


def report(sid, outcome):
    """One line per step: what was asked for, and what the server answered."""
    print(f"\n{sid}")
    for label, code, body in outcome:
        note = ''
        if label.startswith('delete messages') and isinstance(body, dict):
            touched = ', '.join(f"{t}: {n}" for t, n in (body.get('rooms') or {}).items() if n)
            note = f"  {body.get('total', 0)} deleted" + (f" ({touched})" if touched else "")
        elif not 200 <= code < 300:
            note = f"  {str(body)[:120]}"
        print(f"  {label:<22}{code}{note}")


def read_ids(args):
    ids = list(args.session_ids)
    if args.from_file:
        with open(args.from_file) as f:
            for line in f:
                line = line.split('#', 1)[0].strip()
                if line:
                    ids.append(line)

    seen, out = set(), []
    for sid in ids:
        if not SESSION_ID_RE.match(sid):
            raise SogsError(f"Not a 05 Session ID: {sid}")
        sid = sid.lower()
        ed25519_pubkey(sid)
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def read_moderator_key():
    """The signing key from SOGS_MOD_SEED.

    Environment only: the repo's .env is the intended source, sourced into the shell
    the way the other jobs here do it.
    """
    seed = os.environ.get('SOGS_MOD_SEED')
    if not seed:
        raise SogsError(
            "No moderator key: set SOGS_MOD_SEED (the repo .env holds it; "
            "source it with `set -a && . ./.env && set +a`)"
        )
    if not re.fullmatch(r'[0-9a-fA-F]{64}', seed):
        raise SogsError("SOGS_MOD_SEED must be a 64-character hex Ed25519 seed")

    return SigningKey(bytes.fromhex(seed))


def main():
    ap = argparse.ArgumentParser(
        description=f"Ban Session IDs from our communities on {SOGS_URL}, server-wide.",
        epilog="SOGS_MOD_SEED must be the seed of a global moderator or admin of the server.",
    )
    ap.add_argument('session_ids', nargs='*', metavar='SESSION_ID', help="05... Session IDs to ban")
    ap.add_argument('--from-file', metavar='PATH', help="File of Session IDs, one per line")
    ap.add_argument('--unban', action='store_true', help="Lift the bans instead (messages are not restored)")
    ap.add_argument('--keep-messages', action='store_true', help="Do not delete their posts and uploads")
    ap.add_argument('--global-only', action='store_true', help="Apply only the server-wide ban, no per-room bans")
    ap.add_argument('--dry-run', action='store_true', help="Print what would be sent and exit")
    ap.add_argument('--yes', '-y', action='store_true', help="Do not ask for confirmation")
    ap.add_argument('--whoami', action='store_true', help="Print the Session ID of the configured key and exit")
    args = ap.parse_args()

    url, server_pubkey = SOGS_URL, bytes.fromhex(SOGS_PUBKEY)
    signing_key = read_moderator_key()

    if args.whoami:
        print(f"Session ID:  {session_id_of(signing_key)}")
        print("Blinded:     {}\n             {}".format(*blinded_ids(session_id_of(signing_key), server_pubkey)))
        return 0

    ids = read_ids(args)
    if not ids:
        raise SogsError("No Session IDs given")

    caps = capabilities(url)
    blinded = 'blind' in caps
    client = Sogs(url, server_pubkey, signing_key, blinded)

    code, rooms = client.get('/rooms')
    if code == 401:
        raise SogsError(f"The server rejected our authentication: {rooms}")
    if code != 200:
        raise SogsError(f"GET /rooms returned {code}: {rooms}")
    if not any(r.get('global_moderator') or r.get('global_admin') for r in rooms):
        raise SogsError(
            f"{session_id_of(signing_key)} is not a global moderator of {url}.\n"
            "Register it on the server with:\n"
            f"    python3 -msogs --add-moderators {session_id_of(signing_key)} --rooms + --hidden"
        )
    room_tokens = [r['token'] for r in rooms]

    delete_messages = not (args.keep_messages or args.unban)
    verb = 'unban' if args.unban else 'ban'
    steps = ([f"POST /user/<id>/{verb} {{'global': true}}"]
             + ([] if args.global_only else [f"POST /user/<id>/{verb} {{'rooms': ['*']}}"])
             + (["DELETE /rooms/all/<id>, under each id form until one answers"] if delete_messages else []))

    action = 'Unbanning' if args.unban else 'Banning'
    scope = 'server-wide only' if args.global_only else f"server-wide and in {len(room_tokens)} room(s)"
    print(f"{action} {len(ids)} account(s) on {url}, {scope}"
          + (", and deleting all their messages" if delete_messages else ""))
    for step in steps:
        print(f"  {step}")

    if args.dry_run:
        print("\nDry run: nothing was sent.")
        for sid in ids:
            print(f"  {sid}")
        return 0

    if not args.yes:
        if input("Continue? [y/N] ").strip().lower() not in ('y', 'yes'):
            print("Aborted.")
            return 1

    failures = []
    for sid in ids:
        try:
            report(sid, apply_to(client, sid, args.unban, not args.global_only, delete_messages))
        except PartialBan as e:
            report(sid, e.outcome)
            failures.append(str(e))
        except SogsError as e:
            failures.append(str(e))

    for f in failures:
        print(f"\nFAILED: {f}", file=sys.stderr)
    print(f"\n{len(ids) - len(failures)}/{len(ids)} account(s) actioned.")

    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SogsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
