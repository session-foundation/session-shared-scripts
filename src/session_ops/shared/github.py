"""GitHub's REST API for the jobs that publish to the platform repos: who they
authenticate as, and the pull requests they keep open.

Publishing authenticates as a GitHub App, with an installation token minted per run
and scoped to the repositories the job names: it expires in an hour, and pull
requests show as the App rather than as a person.

Config (env vars):
    GITHUB_APP_ID                the App's id
    CREDENTIALS_DIRECTORY        set by systemd's LoadCredential=; holds github-app.pem
"""
import os
import time

import jwt

from session_ops.shared import http

API = "https://api.github.com"
APP_KEY_CREDENTIAL = "github-app.pem"
# Where the publishing units' LoadCredential= reads it from.
APP_KEY_PATH = "/etc/session-ops/github-app.pem"


def session(token):
    s = http.Session()
    s.headers.update({"Authorization": f"Bearer {token}",
                      "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})
    return s


def check(resp, what):
    if resp.status_code >= 400:
        raise RuntimeError(f"GitHub {resp.status_code} {what}: {resp.text[:200]}")
    return resp.json() if resp.content else None


def app_jwt(app_id, private_key, now=None):
    now = int(now or time.time())
    # Backdated a minute for clock drift; GitHub refuses one living past ten minutes.
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                      private_key, algorithm="RS256")


def installation_token(app_id, private_key, owner, repositories, api=None):
    """A one-hour token for the App's installation on `owner`, limited to
    `repositories`."""
    api = api or session(app_jwt(app_id, private_key))
    installation = check(api.request("GET", f"{API}/orgs/{owner}/installation"),
                         f"finding the App's installation on {owner}")
    granted = check(api.request("POST",
                                f"{API}/app/installations/{installation['id']}/access_tokens",
                                json={"repositories": list(repositories)}),
                    "minting an installation token")
    return granted["token"]


def app_private_key():
    """The App's key from systemd's credential store, or None when there is none or
    it is empty: install.sh creates the file empty so the units can start without it."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return None
    try:
        with open(os.path.join(directory, APP_KEY_CREDENTIAL), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except FileNotFoundError:
        return None


def publish_token(owner, repositories):
    """An installation token for `repositories`; exits naming whatever is missing."""
    app_id, key = os.environ.get("GITHUB_APP_ID"), app_private_key()
    missing = []
    if not app_id:
        missing.append("GITHUB_APP_ID is not set")
    if not key:
        missing.append(f"the App's private key is missing or empty ({APP_KEY_PATH}, "
                       f"loaded as the {APP_KEY_CREDENTIAL} credential)")
    if missing:
        raise SystemExit(f"Cannot publish: {'; '.join(missing)}.")
    return installation_token(app_id, key, owner, repositories)


def open_pull(api, repo, head):
    owner = repo.split("/")[0]
    pulls = check(api.request("GET", f"{API}/repos/{repo}/pulls",
                              params={"head": f"{owner}:{head}", "state": "open"}),
                  f"listing {repo}'s pull requests")
    return pulls[0] if pulls else None


def ensure_pull(api, repo, head, base, title, body):
    """Open a pull request from `head`, or retitle the one already open. Returns its URL."""
    existing = open_pull(api, repo, head)
    if existing:
        check(api.request("PATCH", f"{API}/repos/{repo}/pulls/{existing['number']}",
                          json={"title": title, "body": body}),
              f"updating {repo}#{existing['number']}")
        return existing["html_url"]
    created = check(api.request("POST", f"{API}/repos/{repo}/pulls",
                                json={"head": head, "base": base, "title": title,
                                      "body": body}),
                    f"opening a pull request on {repo}")
    return created["html_url"]


def retire_branch(api, repo, branch):
    """Close the pull request from `branch` and delete it: nothing is left to merge."""
    existing = open_pull(api, repo, branch)
    if existing:
        check(api.request("PATCH", f"{API}/repos/{repo}/pulls/{existing['number']}",
                          json={"state": "closed"}),
              f"closing {repo}#{existing['number']}")
    resp = api.request("DELETE", f"{API}/repos/{repo}/git/refs/heads/{branch}")
    if resp.status_code not in (204, 404, 422):
        check(resp, f"deleting {repo}:{branch}")
