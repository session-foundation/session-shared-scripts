"""
    python -m unittest discover        # from sogs_moderation/

The signing vectors are pysogs' own: they are the headers printed by its
contrib/auth-example.py for the seed, server pubkey, nonce and timestamp below. Any
drift in our port of that construction breaks these before it reaches the server.
"""

import contextlib
import io
import unittest
from unittest import mock

from nacl.signing import SigningKey

import ban

SEED = 'c010d89eccbaf5d1c6d19df766c6eedf965d4a28a56f87c9fc819edb59896dd9'
SERVER_PUBKEY = 'c3b3c6f32f0ab5a57f853cc4f30f5da7fda5624b0c77b3fb0829de562ada081d'
NONCE = '09d0799f2295990182c3ab3406fbfc5b'
TIMESTAMP = 1642472103
PATH = '/room/the-best-room/messages/recent?limit=25'

SESSION_ID = '0588672ccb97f40bb57238989226cf429b575ba355443f47bc76c5ab144a96c65b'
BLINDED_ABS = '1598932d4bccbe595a8789d7eb1629cefc483a0eaddc7e20e8fe5c771efafd9a75'
BLINDED_NEG = '1598932d4bccbe595a8789d7eb1629cefc483a0eaddc7e20e8fe5c771efafd9af5'


def headers(blinded, body=None):
    return ban.auth_headers(
        SigningKey(bytes.fromhex(SEED)),
        bytes.fromhex(SERVER_PUBKEY),
        'GET',
        PATH,
        TIMESTAMP,
        bytes.fromhex(NONCE),
        body,
        blinded,
    )


class TestSigning(unittest.TestCase):
    def test_unblinded(self):
        h = headers(blinded=False)
        self.assertEqual(h['X-SOGS-Pubkey'], '00bac6e71efd7dfa4a83c98ed24f254ab2c267f9ccdb172a5280a0444ad24e89cc')
        self.assertEqual(h['X-SOGS-Nonce'], 'CdB5nyKVmQGCw6s0Bvv8Ww==')
        self.assertEqual(h['X-SOGS-Timestamp'], '1642472103')
        self.assertEqual(
            h['X-SOGS-Signature'],
            'xxLpXHbomAJMB9AtGMyqvBsXrdd2040y+Ol/IKzElWfKJa3EYZRv1GLO6CTLhrDFUwVQe8PPltyGs54Kd7O5Cg==',
        )

    def test_blinded(self):
        h = headers(blinded=True)
        self.assertEqual(h['X-SOGS-Pubkey'], BLINDED_NEG)
        self.assertEqual(
            h['X-SOGS-Signature'],
            'gYqpWZX6fnF4Gb2xQM3xaXs0WIYEI49+B8q4mUUEg8Rw0ObaHUWfoWjMHMArAtP9QlORfiydsKWz1o6zdPVeCQ==',
        )

    def test_body_changes_the_signature(self):
        self.assertNotEqual(
            headers(blinded=True)['X-SOGS-Signature'],
            headers(blinded=True, body=b'{"global":true}')['X-SOGS-Signature'],
        )


class TestBlinding(unittest.TestCase):
    def test_session_id_of_seed(self):
        self.assertEqual(ban.session_id_of(SigningKey(bytes.fromhex(SEED))), SESSION_ID)

    def test_both_blinded_variants(self):
        self.assertEqual(
            ban.blinded_ids(SESSION_ID, bytes.fromhex(SERVER_PUBKEY)), (BLINDED_ABS, BLINDED_NEG)
        )

    def test_the_signing_key_blinds_to_one_of_them(self):
        """The key's own blinded pubkey must be one of the two we would look it up under."""
        _, kA = ban.blinded_keys(bytes.fromhex(SERVER_PUBKEY), SigningKey(bytes.fromhex(SEED)))
        self.assertIn('15' + kA.hex(), ban.blinded_ids(SESSION_ID, bytes.fromhex(SERVER_PUBKEY)))


class FakeClient:
    def __init__(self, results, deletes=()):
        self.server_pubkey = bytes.fromhex(SERVER_PUBKEY)
        self.results = results
        self.deletes = list(deletes)
        self.sent = None
        self.tried = []

    def sequence(self, subrequests):
        self.sent = subrequests
        return self.results

    def request(self, method, path, body_json=None):
        self.tried.append(path)
        res = self.deletes.pop(0) if self.deletes else {'code': 404, 'body': 'No such user'}
        return res['code'], res['body']


def ok(body):
    return {'code': 200, 'body': body}


class TestApply(unittest.TestCase):
    def steps(self, **kwargs):
        client = FakeClient([ok({})], [ok({'total': 3, 'rooms': {'session': 3}})])
        ban.apply_to(client, SESSION_ID, **kwargs)
        return [(r['method'], r['path'], r.get('json')) for r in client.sent]

    def test_ban_sequence(self):
        """The ban is server-wide only. The deletion is not in the sequence: it follows,
        under whichever id form the route answers to."""
        self.assertEqual(
            self.steps(unban=False),
            [('POST', f'/user/{SESSION_ID}/ban', {'global': True})],
        )

    def test_unban_sequence(self):
        self.assertEqual(
            self.steps(unban=True),
            [('POST', f'/user/{SESSION_ID}/unban', {'global': True})],
        )

    def test_unban_deletes_nothing(self):
        """An unban that deleted messages would be the opposite of undoing the ban."""
        client = FakeClient([ok({})], [ok({'total': 3, 'rooms': {'session': 3}})])
        outcome = ban.apply_to(client, SESSION_ID, unban=True)
        self.assertEqual([label for label, _, _ in outcome], ['server-wide unban'])
        self.assertEqual(client.tried, [])

    def test_every_step_reports_its_status(self):
        """The statuses are the whole confirmation now, so they have to come back
        attached to the step they belong to."""
        client = FakeClient([ok({})], [ok({'total': 3, 'rooms': {'session': 3}})])
        outcome = ban.apply_to(client, SESSION_ID, unban=False)
        self.assertEqual([(label, code) for label, code, _ in outcome],
                         [('server-wide ban', 200), ('delete messages (05)', 200)])
        self.assertEqual(outcome[-1][2], {'total': 3, 'rooms': {'session': 3}})

    def test_missing_user_still_counts_as_banned(self):
        """A user the server has never seen has nothing to delete; the ban stands, so
        a 404 on that step is not a failure. Every id form has to have been tried
        before that conclusion is drawn."""
        client = FakeClient([ok({})])
        outcome = ban.apply_to(client, SESSION_ID, unban=False)
        self.assertEqual(outcome[-1][:1] + outcome[-1][2:],
                         ('delete messages (05/15)', {'total': 0, 'rooms': {}}))
        self.assertEqual(client.tried,
                         [f'/rooms/all/{i}' for i in (SESSION_ID, BLINDED_ABS, BLINDED_NEG)])

    def test_deletion_falls_back_to_the_blinded_id(self):
        """Our server 404s /rooms/all/ on an 05 id — the route itself does not match —
        and that answer is indistinguishable from an account it has never seen."""
        client = FakeClient(
            [ok({})],
            [{'code': 404, 'body': ''}, ok({'total': 2, 'rooms': {'session-updates': 2}})],
        )
        outcome = ban.apply_to(client, SESSION_ID, unban=False)
        self.assertEqual(outcome[-1][0], 'delete messages (15)')
        self.assertEqual(outcome[-1][2]['total'], 2)
        self.assertEqual(client.tried, [f'/rooms/all/{SESSION_ID}', f'/rooms/all/{BLINDED_ABS}'])

    def test_a_refused_deletion_is_a_partial_ban(self):
        client = FakeClient([ok({})], [{'code': 403, 'body': 'nope'}])
        with self.assertRaises(ban.PartialBan) as e:
            ban.apply_to(client, SESSION_ID, unban=False)
        self.assertEqual(e.exception.outcome[-1][:2], ('delete messages (05)', 403))

    def test_a_refused_ban_carries_what_did_run(self):
        """The ban is refused, so nothing should have been deleted: the deletion must
        not run for an account the server would not let us ban."""
        client = FakeClient([{'code': 403, 'body': 'This endpoint requires moderator permissions'}])
        with self.assertRaises(ban.PartialBan) as e:
            ban.apply_to(client, SESSION_ID, unban=False)
        self.assertEqual([(label, code) for label, code, _ in e.exception.outcome],
                         [('server-wide ban', 403)])
        self.assertEqual(e.exception.total_steps, 2)
        self.assertIn('1/2', str(e.exception))
        self.assertEqual(client.tried, [])

    def test_a_sequence_cut_short_is_a_partial_ban(self):
        """/sequence answers with fewer results than it was sent when it aborts."""
        client = FakeClient([])
        with self.assertRaises(ban.PartialBan):
            ban.apply_to(client, SESSION_ID, unban=True)


class TestReport(unittest.TestCase):
    """The per-step block is what gets pasted into the ticket reply, so a failure must
    not be able to render as a count."""

    def render(self, outcome):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ban.report(SESSION_ID, outcome)
        return out.getvalue()

    def test_a_refused_deletion_shows_the_error_not_a_count(self):
        line = self.render([('delete messages (15)', 403, {'error': 'forbidden'})])
        self.assertNotIn('deleted', line)
        self.assertIn('forbidden', line)

    def test_a_successful_deletion_shows_the_count_and_rooms(self):
        line = self.render([('delete messages (15)', 200,
                             {'total': 3, 'rooms': {'session-updates': 3, 'quiet': 0}})])
        self.assertIn('3 deleted', line)
        self.assertIn('session-updates: 3', line)
        self.assertNotIn('quiet', line)

    def test_an_every_form_404_is_a_count_not_an_error(self):
        """No account under any id form leaves the ban standing with nothing to delete."""
        line = self.render([('delete messages (05/15)', 404, {'total': 0, 'rooms': {}})])
        self.assertIn('0 deleted', line)


class TestIds(unittest.TestCase):
    def parse(self, *ids):
        return ban.read_ids(type('Args', (), {'session_ids': list(ids), 'from_file': None})())

    def test_dedupes_and_lowercases(self):
        self.assertEqual(self.parse(SESSION_ID.upper(), SESSION_ID), [SESSION_ID])

    def test_rejects_blinded_id(self):
        with self.assertRaises(ban.SogsError):
            self.parse(BLINDED_ABS)

    def test_rejects_truncated_id(self):
        with self.assertRaises(ban.SogsError):
            self.parse(SESSION_ID[:-1])


class TestModeratorKey(unittest.TestCase):
    """SOGS_MOD_SEED is the whole configuration surface, so a bad one has to fail
    before anything is fetched rather than sign requests with the wrong identity."""

    def read(self, value):
        env = {} if value is None else {'SOGS_MOD_SEED': value}
        with mock.patch.dict(ban.os.environ, env, clear=True):
            return ban.read_moderator_key()

    def test_a_valid_seed_yields_the_matching_session_id(self):
        self.assertEqual(ban.session_id_of(self.read(SEED)),
                         ban.session_id_of(SigningKey(bytes.fromhex(SEED))))

    def test_a_missing_seed_names_where_it_lives(self):
        with self.assertRaises(ban.SogsError) as e:
            self.read(None)
        self.assertIn('.env', str(e.exception))

    def test_a_seed_of_the_wrong_length_is_refused(self):
        for bad in (SEED[:-1], SEED + 'ab', '', 'not-hex' * 8):
            with self.assertRaises(ban.SogsError):
                self.read(bad)


class TestConfirm(unittest.TestCase):
    def test_yes_skips_the_prompt(self):
        with mock.patch('builtins.input', side_effect=AssertionError("should not prompt")):
            self.assertTrue(ban.confirm(True))

    def test_a_closed_stdin_refuses_instead_of_raising(self):
        """These run from cron and CI, where input() hits EOF and would otherwise leave
        a traceback rather than an aborted run."""
        with mock.patch('builtins.input', side_effect=EOFError):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(ban.confirm(False))

    def test_anything_but_yes_refuses(self):
        for answer in ('', 'n', 'no', 'Y E S', ' '):
            with mock.patch('builtins.input', return_value=answer):
                self.assertFalse(ban.confirm(False))

    def test_y_and_yes_accept(self):
        for answer in ('y', 'YES', ' Yes '):
            with mock.patch('builtins.input', return_value=answer):
                self.assertTrue(ban.confirm(False))


class TestServerIsFixed(unittest.TestCase):
    """The server is a constant, not a flag: the only thing retargeting this tool can
    add is a bulk ban on somebody else's server."""

    def test_the_pubkey_is_64_hex_characters(self):
        self.assertRegex(ban.SOGS_PUBKEY, r'\A[0-9a-f]{64}\Z')

    def test_the_url_carries_no_path_or_query(self):
        """Nothing trims this constant, so it has to already be scheme://netloc."""
        self.assertEqual(ban.SOGS_URL, 'https://open.getsession.org')

    def test_no_flag_can_retarget_the_server(self):
        """The flag set is asserted whole rather than blocklisted by name: a blocklist
        passes anything spelled differently, which is the way this would actually be
        reintroduced."""
        self.assertEqual(
            {a.dest for a in ban.build_parser()._actions},
            {'help', 'session_ids', 'from_file', 'unban', 'dry_run', 'yes', 'whoami'},
        )


if __name__ == '__main__':
    unittest.main()
