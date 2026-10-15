"""The latest version each release channel publishes, normalised to plain "1.2.3".

Every fetcher takes a shared http.Session and returns a Release, or raises: the job
records a failure per source rather than per platform, since several platforms
share one.
"""
import functools
import gzip
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from session_ops.shared import github

PLAY_DETAILS = "https://play.google.com/store/apps/details"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
FLATHUB_APPSTREAM = "https://flathub.org/api/v2/appstream/{app_id}"
FLATHUB_PAGE = "https://flathub.org/apps/{app_id}"
FLATHUB_METAINFO = "https://raw.githubusercontent.com/{repo}/{branch}/{app_id}.metainfo.xml"

# Play has no version API: the listing bootstraps its data with the version as
# `[[["1.2.3"]]`. When Android starts failing, Google has likely changed the page.
PLAY_VERSION = re.compile(r'\[\[\["(\d+(?:\.\d+)+)"\]\]')
DEB_EPOCH = re.compile(r"^\d+:")
DEB_REVISION = re.compile(r"-[^-]+$")
RELEASE = re.compile(r"(\d+(?:\.\d+)*)(.*)", re.DOTALL)
MAX_VERSION_CHARS = 64


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    url: str = ""
    # GitHub only: the published prereleases above `version`.
    prereleases: tuple = ()


def normalize(version):
    return re.sub(r"^[vV]", "", (version or "").strip())


def version_key(version):
    """Orders dotted versions numerically, so 1.10.0 sorts after 1.9.0 and 1.2 equals
    1.2.0, and ranks a suffixed one (1.2.3-beta1, 1.2.3rc1) below its bare release."""
    match = RELEASE.match(version)
    if not match:
        return ((), 0, ())
    parts = [int(part) for part in match.group(1).split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    suffix = match.group(2)
    return (tuple(parts), 0 if suffix else 1,
            tuple(int(part) for part in re.findall(r"\d+", suffix)))


def compare(a, b):
    """-1, 0 or 1 for a < b, a == b, a > b."""
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def newest(versions):
    """The highest of `versions`, None if there are none. Store lists are not trusted
    to be newest-first: AppStream recommends it and does not require it."""
    versions = [normalize(v) for v in versions if v]
    return max(versions, key=functools.cmp_to_key(compare)) if versions else None


def deb_upstream(version):
    """A Debian version without its epoch ("1:") or revision ("-2"); the versions
    tracked here never contain a hyphen of their own."""
    version = DEB_EPOCH.sub("", version.strip())
    return normalize(DEB_REVISION.sub("", version) if "-" in version else version)


def checked_version(release, what):
    """`release`, if its version is one an embed can carry and compare() can order."""
    if not RELEASE.match(release.version) or len(release.version) > MAX_VERSION_CHARS:
        raise SourceError(f"{what} gave an unusable version: {release.version[:MAX_VERSION_CHARS]!r}")
    return release


def checked(resp, what):
    if resp.status_code >= 400:
        raise SourceError(f"{what}: HTTP {resp.status_code}")
    return resp


def fetch_github(session, repo):
    """The highest release, rather than GitHub's "latest": that is the most recently
    created one, which goes backwards when an older line gets a hotfix."""
    found = [(Release(normalize(tag), url), prerelease)
             for tag, url, prerelease in github.releases(session, f"{github.ORG}/{repo}")]
    released = [release for release, prerelease in found if not prerelease]
    if not released:
        raise SourceError(f"none of GitHub {repo}'s last {github.RELEASES_PAGE} releases "
                          "is a full release")
    by_version = functools.cmp_to_key(lambda a, b: compare(a.version, b.version))
    latest = checked_version(max(released, key=by_version), f"GitHub {repo}")
    above = tuple(release for release, prerelease in found
                  if prerelease and compare(release.version, latest.version) > 0)
    return Release(latest.version, latest.url, above)


def fetch_play(session, app_id, country="us", lang="en"):
    resp = checked(session.request("GET", PLAY_DETAILS,
                                   params={"id": app_id, "hl": lang, "gl": country}),
                   f"Play listing for {app_id}")
    match = PLAY_VERSION.search(resp.text)
    if not match:
        raise SourceError(f"no version in the Play listing for {app_id}; "
                          "the page layout may have changed")
    return Release(match.group(1), f"{PLAY_DETAILS}?id={app_id}")


def fetch_appstore(session, bundle_id, country="us"):
    resp = checked(session.request("GET", ITUNES_LOOKUP,
                                   params={"bundleId": bundle_id, "country": country}),
                   f"App Store lookup for {bundle_id}")
    results = resp.json().get("results") or []
    if not results or not results[0].get("version"):
        raise SourceError(f"the App Store lists no version for {bundle_id}")
    return Release(normalize(results[0]["version"]), results[0].get("trackViewUrl", ""))


def fetch_deb(session, base_url, package, suite="sid", component="main", arch="amd64"):
    base = base_url.rstrip("/")
    index = f"{base}/dists/{suite}/{component}/binary-{arch}/Packages.gz"
    resp = checked(session.request("GET", index), index)
    try:
        text = gzip.decompress(resp.content).decode("utf-8", "replace")
    except (OSError, EOFError) as exc:
        raise SourceError(f"cannot decompress {index}: {exc}") from exc
    version = packages_version(text, package)
    if version is None:
        raise SourceError(f"no {package} in {index}")
    return Release(deb_upstream(version), base)


def packages_version(text, package):
    """The Version: of `package` in a Packages index, or None."""
    for stanza in re.split(r"\n\s*\n", text):
        fields = dict(line.split(":", 1) for line in stanza.splitlines()
                      if ":" in line and not line.startswith(" "))
        if fields.get("Package", "").strip() == package and fields.get("Version"):
            return fields["Version"].strip()
    return None


def fetch_fdroid(session, base_url, app_id):
    base = base_url.rstrip("/")
    resp = checked(session.request("GET", f"{base}/index-v1.json"), f"{base}/index-v1.json")
    try:
        index = resp.json()
    except ValueError as exc:
        raise SourceError(f"cannot parse {base}/index-v1.json: {exc}") from exc
    version = fdroid_version(index, app_id)
    if version is None:
        raise SourceError(f"no {app_id} in {base}/index-v1.json")
    return Release(normalize(version), base)


def fdroid_version(index, app_id):
    """versionName of the build with the highest versionCode: one entry per ABI split."""
    builds = (index.get("packages") or {}).get(app_id)
    if not builds:
        return None
    return max(builds, key=lambda build: build.get("versionCode", 0)).get("versionName")


def fetch_flathub(session, app_id):
    resp = checked(session.request("GET", FLATHUB_APPSTREAM.format(app_id=app_id)),
                   f"Flathub appstream for {app_id}")
    version = newest(release.get("version") for release in resp.json().get("releases") or [])
    if version is None:
        raise SourceError(f"Flathub lists no release of {app_id}")
    return Release(version, FLATHUB_PAGE.format(app_id=app_id))


def fetch_flathub_manifest(session, app_id, repo=None, branch="master"):
    repo = repo or f"flathub/{app_id}"
    url = FLATHUB_METAINFO.format(repo=repo, branch=branch, app_id=app_id)
    version = metainfo_version(checked(session.request("GET", url), url).content)
    if version is None:
        raise SourceError(f"no <release version> in {url}")
    return Release(version, f"https://github.com/{repo}")


def metainfo_version(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SourceError(f"cannot parse metainfo: {exc}") from exc
    return newest(release.get("version") for release in root.iterfind("releases/release"))


FETCHERS = {
    "play": fetch_play,
    "appstore": fetch_appstore,
    "deb": fetch_deb,
    "fdroid": fetch_fdroid,
    "flathub": fetch_flathub,
    "flathub_manifest": fetch_flathub_manifest,
}


def fetch_store(session, store):
    """`store` is a platform's store spec: its `kind`, and that fetcher's arguments."""
    arguments = dict(store)
    kind = arguments.pop("kind")
    return checked_version(FETCHERS[kind](session, **arguments), kind)
