"""
    uv run python -m unittest tests.sogs.test_names
"""

import unittest
from base64 import b64encode

from nacl.signing import SigningKey

from session_ops.sogs import ban, names
from session_ops.sogs.errors import SogsError
from tests.sogs.test_ban import BLINDED_ABS, SERVER_PUBKEY, SESSION_ID

OTHER_ID = ban.session_id_of(SigningKey(bytes(range(32))))


def field(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def varint(n):
    out = b''
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def message_data(name, pad=True):
    profile = field(1, name.encode())
    data_message = field(1, b'hello') + field(101, profile)
    content = field(1, data_message)
    if pad:
        content += b'\x80' + b'\x00' * (160 - len(content) - 1)
    return b64encode(content).decode()


def post(sid, posted, data):
    return {'session_id': sid, 'posted': posted, 'data': data}


def owners(*sids):
    pub = bytes.fromhex(SERVER_PUBKEY)
    return {form: sid for sid in sids for form in (sid, *ban.blinded_ids(sid, pub))}


class FakeClient:
    def __init__(self, by_room):
        self.by_room = by_room

    def request(self, method, path, body_json=None):
        answer = self.by_room[path.split('/')[2]]
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestDisplayName(unittest.TestCase):
    def test_reads_the_name_from_padded_content(self):
        self.assertEqual(names.display_name(message_data('Mallory')), 'Mallory')

    def test_reads_it_without_padding(self):
        self.assertEqual(names.display_name(message_data('Mallory', pad=False)), 'Mallory')

    def test_content_without_a_profile_has_no_name(self):
        content = field(1, field(1, b'hello'))
        self.assertIsNone(names.display_name(b64encode(content).decode()))

    def test_garbage_is_no_name_rather_than_an_error(self):
        self.assertIsNone(names.display_name(b64encode(b'\x0a\xff\xff').decode()))
        self.assertIsNone(names.display_name('not base64!'))


class TestRecentNames(unittest.TestCase):
    def test_the_newest_post_names_the_account(self):
        client = FakeClient({
            'a': (200, [post(BLINDED_ABS, 10, message_data('Old')), post(BLINDED_ABS, 30, message_data('New'))]),
            'b': (200, [post(BLINDED_ABS, 20, message_data('Middle'))]),
        })
        found, unread = names.recent_names(client, [{'token': 'a'}, {'token': 'b'}], owners(SESSION_ID))
        self.assertEqual(found, {SESSION_ID: 'New'})
        self.assertEqual(unread, [])

    def test_an_account_that_did_not_post_is_absent(self):
        client = FakeClient({'a': (200, [post(BLINDED_ABS, 10, message_data('Someone'))])})
        found, _ = names.recent_names(client, [{'token': 'a'}], owners(OTHER_ID))
        self.assertEqual(found, {})

    def test_a_deleted_post_is_skipped(self):
        client = FakeClient({'a': (200, [post(BLINDED_ABS, 10, None)])})
        found, _ = names.recent_names(client, [{'token': 'a'}], owners(SESSION_ID))
        self.assertEqual(found, {})

    def test_an_unreadable_room_is_reported(self):
        client = FakeClient({'a': (403, 'nope'), 'b': (200, [])})
        _, unread = names.recent_names(client, [{'token': 'a'}, {'token': 'b'}], owners(SESSION_ID))
        self.assertEqual(unread, [('a', 403)])


class TestUnreachableRoom(unittest.TestCase):
    def test_a_room_that_times_out_is_reported_not_raised(self):
        """The lookup is informational; a timeout on one room must not stop the ban."""
        client = FakeClient({'a': SogsError('GET /room/a failed: timed out'),
                             'b': (200, [post(BLINDED_ABS, 10, message_data('Mallory'))])})
        found, unread = names.recent_names(client, [{'token': 'a'}, {'token': 'b'}],
                                           owners(SESSION_ID))
        self.assertEqual(found, {SESSION_ID: 'Mallory'})
        self.assertEqual([t for t, _ in unread], ['a'])


class TestDescribePoster(unittest.TestCase):
    def test_each_case_reads_differently(self):
        self.assertEqual(ban.describe_poster(SESSION_ID, {}), "no recent posts")
        self.assertEqual(ban.describe_poster(SESSION_ID, {SESSION_ID: None}), "posted, with no display name")
        self.assertEqual(ban.describe_poster(SESSION_ID, {SESSION_ID: 'Mallory'}), "posted as 'Mallory'")


if __name__ == '__main__':
    unittest.main()
