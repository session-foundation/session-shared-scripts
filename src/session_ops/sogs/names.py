"""
The display names accounts last posted under, read from our rooms' recent messages.

SOGS stores no profiles: a name exists on the server only inside the messages an account
has posted, as the profile its client attaches to each one. An account that has not
posted recently has no name to find.
"""

from base64 import b64decode

from session_ops.sogs.errors import SogsError

# The most GET /room/<token>/messages/recent returns.
RECENT_LIMIT = 256

CONTENT_DATA_MESSAGE = 1
DATA_MESSAGE_PROFILE = 101
PROFILE_DISPLAY_NAME = 1


def _varint(buf, i):
    out = shift = 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return out, i


def _fields(buf):
    """(field number, value) for each field of a protobuf message; bytes for
    length-delimited fields, which is all a name lookup descends into."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, i = _varint(buf, i)
        elif wire_type == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire_type == 2:
            size, i = _varint(buf, i)
            value, i = buf[i:i + size], i + size
        elif wire_type == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        yield number, value


def _field(buf, number):
    return next((v for n, v in _fields(buf) if n == number and isinstance(v, bytes)), None)


def display_name(data_b64):
    """The display name in a SOGS message's `data`, or None if it carries none or does
    not parse."""
    try:
        raw = b64decode(data_b64)
    except ValueError:
        return None
    # Clients pad the Content with 0x80 and then zeros.
    raw = raw.rstrip(b'\x00')
    if raw.endswith(b'\x80'):
        raw = raw[:-1]
    try:
        name = raw
        for number in (CONTENT_DATA_MESSAGE, DATA_MESSAGE_PROFILE, PROFILE_DISPLAY_NAME):
            name = _field(name, number)
            if name is None:
                return None
    except (IndexError, ValueError):
        return None
    return name.decode('utf-8', 'replace') or None


def recent_names(client, rooms, owners):
    """Looks accounts up in every room's recent messages.

    `owners` maps each id form an account can post under (05 and both blinded 15
    forms) to the 05 id to report it as. Returns ({id: name}, [(room token, why)]).
    An id that posted is in the dict, with the name from its newest post (None if
    that post carried none); an id that did not is absent. The list names the rooms
    that could not be read, so a missing id is not mistaken for one that never
    posted. Never raises: the lookup is informational and must not stop a ban.
    """
    newest, unread = {}, []
    for room in rooms:
        token = room['token']
        try:
            code, messages = client.request('GET', f'/room/{token}/messages/recent?limit={RECENT_LIMIT}')
        except SogsError as e:
            unread.append((token, str(e)))
            continue
        if code != 200 or not isinstance(messages, list):
            unread.append((token, code))
            continue
        for m in messages:
            sid = owners.get(m.get('session_id'))
            # A deleted message stays in the list with its data nulled.
            if not sid or not m.get('data'):
                continue
            if sid not in newest or m['posted'] > newest[sid][0]:
                newest[sid] = (m['posted'], display_name(m['data']))
    return {sid: name for sid, (_, name) in newest.items()}, unread
