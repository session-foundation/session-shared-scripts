"""
    uv run python -m unittest tests.store_versions.test_sources
"""
import gzip
import unittest

from session_ops.shared.testing import FakeResponse, FakeSession
from session_ops.store_versions import sources


class TestVersions(unittest.TestCase):
    def test_compare_is_numeric(self):
        self.assertEqual(sources.compare("1.18.1", "1.17.17"), 1)
        self.assertEqual(sources.compare("1.2.0", "1.10.0"), -1)
        self.assertEqual(sources.compare("2.15.3", "2.15.3"), 0)
        self.assertEqual(sources.compare("1.2", "1.2.0"), 0)

    def test_a_suffixed_version_is_below_its_release(self):
        self.assertEqual(sources.compare("1.2.3-beta1", "1.2.3"), -1)
        self.assertEqual(sources.compare("1.2.3rc1", "1.2.3"), -1)
        self.assertEqual(sources.compare("1.2.3-beta1", "1.2.3-beta2"), -1)
        self.assertEqual(sources.compare("1.2.3-beta1", "1.2.2"), 1)
        self.assertEqual(sources.newest(["1.34.0-rc1", "1.34.0"]), "1.34.0")

    def test_an_unusable_version_fails_the_source(self):
        for version in ("", "v", "beta", "1" * 65):
            with self.subTest(version=version), self.assertRaises(sources.SourceError):
                sources.checked_version(sources.Release(version), "test")
        self.assertEqual(sources.checked_version(sources.Release("1.2.3"), "test").version, "1.2.3")

    def test_normalize_and_deb_upstream(self):
        self.assertEqual(sources.normalize(" v1.18.1 "), "1.18.1")
        self.assertEqual(sources.deb_upstream("1:1.17.17-2"), "1.17.17")
        self.assertEqual(sources.deb_upstream("1.18.2"), "1.18.2")

    def test_newest_ignores_list_order(self):
        self.assertEqual(sources.newest(["1.9.0", "v1.10.0", None, ""]), "1.10.0")
        self.assertIsNone(sources.newest([]))


class TestParsers(unittest.TestCase):
    PACKAGES = ("Package: session-messenger-desktop\nVersion: 1.0.0\n\n"
                "Package: session-desktop\nArchitecture: amd64\nVersion: 1:1.18.2-1\n"
                "Description: Session\n multi-line\n")

    def test_packages_finds_the_exact_package(self):
        self.assertEqual(sources.packages_version(self.PACKAGES, "session-desktop"), "1:1.18.2-1")
        self.assertIsNone(sources.packages_version(self.PACKAGES, "nope"))

    def test_packages_takes_the_highest_of_several_versions(self):
        text = ("Package: session-desktop\nVersion: 1.18.2\n\n"
                "Package: session-desktop\nVersion: 1.18.10\n\n"
                "Package: session-desktop\nVersion: 1.9.0\n")
        self.assertEqual(sources.packages_version(text, "session-desktop"), "1.18.10")

    def test_fdroid_takes_the_highest_version_code(self):
        index = {"packages": {"app": [{"versionCode": 9, "versionName": "9.0.0"},
                                      {"versionCode": 10, "versionName": "10.0.0"},
                                      {"versionCode": 2, "versionName": "2.0.0"}]}}
        self.assertEqual(sources.fdroid_version(index, "app"), "10.0.0")
        self.assertIsNone(sources.fdroid_version(index, "other"))
        self.assertIsNone(sources.fdroid_version({}, "app"))

    def test_metainfo_takes_the_newest_release(self):
        xml = (b'<component><releases><release version="1.18.1"/>'
               b'<release version="1.18.2"><description/></release>'
               b'<release version="1.9.0"/></releases></component>')
        self.assertEqual(sources.metainfo_version(xml), "1.18.2")
        self.assertIsNone(sources.metainfo_version(b"<component/>"))
        with self.assertRaises(sources.SourceError):
            sources.metainfo_version(b"<component>")


class Raw(FakeResponse):
    def __init__(self, text=None, content=None, status_code=200):
        super().__init__({}, status_code)
        self.text = text or ""
        self._content = content

    @property
    def content(self):
        return self._content if self._content is not None else self.text.encode()


class TestFetchers(unittest.TestCase):
    def test_play_extracts_the_bootstrapped_version(self):
        session = FakeSession([Raw('junk [[["1.33.6"]],[[[33]]]] junk')])
        release = sources.fetch_play(session, "network.loki.messenger")
        self.assertEqual(release.version, "1.33.6")
        self.assertEqual(session.calls[0][2]["params"]["id"], "network.loki.messenger")

    def test_play_without_a_version_fails(self):
        with self.assertRaises(sources.SourceError):
            sources.fetch_play(FakeSession([Raw("<html></html>")]), "x")

    def test_deb(self):
        packages = gzip.compress(TestParsers.PACKAGES.encode())
        release = sources.fetch_store(
            FakeSession([Raw(content=packages)]),
            {"kind": "deb", "base_url": "https://deb.example/", "package": "session-desktop"})
        self.assertEqual(release.version, "1.18.2")

    def test_an_empty_fdroid_version_fails_the_source(self):
        index = {"packages": {"a": [{"versionCode": 1, "versionName": ""}]}}
        with self.assertRaises(sources.SourceError):
            sources.fetch_store(FakeSession([FakeResponse(index)]),
                                {"kind": "fdroid", "base_url": "https://f.example", "app_id": "a"})

    def test_flathub_appstream(self):
        session = FakeSession([FakeResponse({"releases": [{"version": "1.18.1"},
                                                          {"version": "1.18.2"}]})])
        release = sources.fetch_flathub(session, "network.loki.Session")
        self.assertEqual(release.version, "1.18.2")
        self.assertEqual(release.url, "https://flathub.org/apps/network.loki.Session")

    def test_github_strips_the_v(self):
        session = FakeSession([FakeResponse([{"tag_name": "v1.18.2", "html_url": "u"}])])
        release = sources.fetch_github(session, "session-desktop")
        self.assertEqual((release.version, release.url), ("1.18.2", "u"))
        self.assertTrue(session.calls[0][1].endswith(
            "/repos/session-foundation/session-desktop/releases"))

    def test_github_takes_the_highest_release_not_the_newest(self):
        session = FakeSession([FakeResponse([
            {"tag_name": "v1.32.3", "html_url": "hotfix"},
            {"tag_name": "v1.33.1", "html_url": "latest"},
            {"tag_name": "v1.32.2", "html_url": "old"}])])
        self.assertEqual(sources.fetch_github(session, "session-android").url, "latest")

    def test_github_keeps_the_prereleases_above_the_release(self):
        session = FakeSession([FakeResponse([
            {"tag_name": "v1.34.0", "html_url": "draft", "draft": True},
            {"tag_name": "v1.33.0", "html_url": "pre", "prerelease": True},
            {"tag_name": "v1.32.2", "html_url": "latest"},
            {"tag_name": "v1.32.0", "html_url": "old pre", "prerelease": True}])])
        release = sources.fetch_github(session, "session-android")
        self.assertEqual(release.version, "1.32.2")
        self.assertEqual(release.prereleases, (sources.Release("1.33.0", "pre"),))

    def test_github_without_a_full_release_fails_the_source(self):
        session = FakeSession([FakeResponse([{"tag_name": "v1.33.0", "prerelease": True}])])
        with self.assertRaises(sources.SourceError):
            sources.fetch_github(session, "session-android")

    def test_http_error_fails_the_source(self):
        with self.assertRaises(sources.SourceError):
            sources.fetch_appstore(FakeSession([FakeResponse({}, status_code=503)]), "x")
