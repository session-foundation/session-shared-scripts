"""
    uv run python -m unittest tests.sogs.test_perms
"""

import unittest

from session_ops.sogs import perms
from tests.sogs.test_ban import BLINDED_ABS, BLINDED_NEG, SERVER_PUBKEY, SESSION_ID


def args(**kwargs):
    return type('Args', (), {p: kwargs.get(p) for p in perms.PERMISSIONS})()


class ProbeClient:
    """Answers the inbox probe per blinded id, as the server would."""

    def __init__(self, answers):
        self.server_pubkey = bytes.fromhex(SERVER_PUBKEY)
        self.answers = answers
        self.sent = []

    def request(self, method, path, body_json=None):
        self.sent.append((method, path, body_json))
        return self.answers[path.rsplit('/', 1)[1]], ''


class TestPermissionBody(unittest.TestCase):
    def test_on_and_off(self):
        self.assertEqual(perms.permission_body(args(write='on', upload='off')),
                         {'write': True, 'upload': False})

    def test_default_uses_the_default_flag(self):
        """The endpoint clears an override through default_<name>, not a null value."""
        self.assertEqual(perms.permission_body(args(write='default')), {'default_write': True})

    def test_unmentioned_permissions_are_left_alone(self):
        self.assertEqual(perms.permission_body(args(read='on')), {'read': True})


class TestResolveBlindedId(unittest.TestCase):
    def test_picks_the_candidate_the_server_knows(self):
        client = ProbeClient({BLINDED_ABS: 404, BLINDED_NEG: 400})
        self.assertEqual(perms.resolve_blinded_id(client, SESSION_ID), BLINDED_NEG)

    def test_probe_sends_an_empty_body(self):
        """An empty body is what makes the probe read-only: the 400 lands before the
        message is read, so nothing is delivered."""
        client = ProbeClient({BLINDED_ABS: 400, BLINDED_NEG: 404})
        perms.resolve_blinded_id(client, SESSION_ID)
        self.assertEqual([(m, b) for m, _, b in client.sent], [('POST', {}), ('POST', {})])

    def test_unknown_account_is_refused_rather_than_guessed(self):
        """Granting to the wrong blinded id creates that account and looks like
        success, so an unresolved id must stop the run."""
        client = ProbeClient({BLINDED_ABS: 404, BLINDED_NEG: 404})
        with self.assertRaises(perms.SogsError):
            perms.resolve_blinded_id(client, SESSION_ID)

    def test_unexpected_status_is_not_read_as_absence(self):
        client = ProbeClient({BLINDED_ABS: 403, BLINDED_NEG: 404})
        with self.assertRaises(perms.SogsError):
            perms.resolve_blinded_id(client, SESSION_ID)


if __name__ == '__main__':
    unittest.main()
