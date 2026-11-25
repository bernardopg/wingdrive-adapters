#!/usr/bin/env python3
"""GitHub adapter for WingDrive (contract 1).

Indexes a GitHub account: the repositories it owns, the ones it starred and,
with a token, the issues and pull requests it takes part in. Every item is one
record of the `item` model, told apart by `kind`.

Repositories are listed in full on each sync (a few requests even for large
accounts) so unstarred or deleted ones disappear; the cursor keeps each
repository's `updated_at` so only changed ones are written again. Issues and
pull requests come incrementally through `/issues?since=`.

Only the Python standard library is used.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
CURSOR_VERSION = 1
MAX_BODY = 100_000
MAX_PAGES = 200
USER_AGENT = "WingDrive-adapter-github/1.0"


class InputError(Exception):
    """A problem the user can fix: printed to stderr, exit 2."""


class RateLimited(Exception):
    """GitHub refused more requests for now; the sync stops and resumes next time."""


def emit(operation):
    sys.stdout.write(json.dumps(operation, ensure_ascii=False) + "\n")


def log(level, message):
    emit({"log": level, "message": message})


# ── HTTP ────────────────────────────────────────────────────────────────────


def api_base():
    """The API root. Tests point it at a local server; nothing else may."""
    override = os.environ.get("WINGDRIVE_GITHUB_API", "")
    if override.startswith("http://127.0.0.1:"):
        return override.rstrip("/")
    return API


class Client:
    def __init__(self, token):
        self.base = api_base()
        self.token = token
        self.requests = 0

    def get(self, url):
        """GET a URL under the API root; returns (json, next page URL or None)."""
        if not url.startswith(self.base + "/"):
            raise ValueError(f"refusing to fetch {url}")
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", USER_AGENT)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
                return body, next_link(response.headers.get("Link", ""))
        except urllib.error.HTTPError as error:
            if error.code in (403, 429) and error.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(error.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(0, reset - int(time.time()))
                raise RateLimited(f"GitHub rate limit reached; it resets in {wait // 60 + 1} min") from None
            if error.code == 401:
                raise InputError("GitHub refused the token: check that it is valid and not expired") from None
            if error.code == 404 and "/users/" in url:
                raise InputError("GitHub has no user with that name") from None
            raise

    def pages(self, path, params):
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        for _ in range(MAX_PAGES):
            body, url = self.get(url)
            yield body
            if not url:
                return


def next_link(header):
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


# ── Records ─────────────────────────────────────────────────────────────────


def repo_record(repo, relation):
    topics = repo.get("topics") or []
    description = repo.get("description") or ""
    lines = [description] if description else []
    if topics:
        lines.append("Topics: " + ", ".join(topics))
    details = []
    if repo.get("language"):
        details.append(repo["language"])
    details.append(f"{repo.get('stargazers_count', 0)} stars")
    if repo.get("fork"):
        details.append("fork")
    if repo.get("archived"):
        details.append("archived")
    lines.append(" · ".join(details))
    return {
        "kind": "repository",
        "title": repo["full_name"],
        "body": "\n".join(lines),
        "repo": repo["full_name"],
        "state": "private" if repo.get("private") else "public",
        "author": (repo.get("owner") or {}).get("login", ""),
        "labels": ", ".join(topics),
        "relation": relation,
        "url": repo.get("html_url", ""),
        "created": repo.get("created_at"),
        "updated": repo.get("pushed_at") or repo.get("updated_at"),
    }


def issue_record(issue):
    repo = (issue.get("repository") or {}).get("full_name") or repo_from_url(issue.get("repository_url", ""))
    is_pull = "pull_request" in issue
    state = issue.get("state", "")
    if is_pull and (issue.get("pull_request") or {}).get("merged_at"):
        state = "merged"
    body = issue.get("body") or ""
    return {
        "kind": "pull_request" if is_pull else "issue",
        "title": issue.get("title", ""),
        "body": body if len(body) <= MAX_BODY else body[:MAX_BODY],
        "repo": repo,
        "number": issue.get("number"),
        "state": state,
        "author": (issue.get("user") or {}).get("login", ""),
        "labels": ", ".join(label.get("name", "") for label in issue.get("labels") or []),
        "relation": "involved",
        "url": issue.get("html_url", ""),
        "created": issue.get("created_at"),
        "updated": issue.get("updated_at"),
    }


def repo_from_url(url):
    match = re.search(r"/repos/([^/]+/[^/]+)$", url)
    return match.group(1) if match else ""


# ── Sync ────────────────────────────────────────────────────────────────────


def flag(config, key, default):
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off")
    return bool(value)


def load_cursor(raw):
    try:
        cursor = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return cursor if isinstance(cursor, dict) and cursor.get("v") == CURSOR_VERSION else {}


def list_repos(client, username, authenticated, starred):
    """Yield (record id, repo, relation) for owned and, if asked, starred repositories."""
    if authenticated:
        owned = client.pages("/user/repos", {"affiliation": "owner", "per_page": 100, "sort": "full_name"})
    else:
        owned = client.pages(f"/users/{username}/repos", {"type": "owner", "per_page": 100})
    for page in owned:
        for repo in page:
            yield f"repo:{repo['id']}", repo, "owner"
    if starred:
        path = "/user/starred" if authenticated else f"/users/{username}/starred"
        for page in client.pages(path, {"per_page": 100}):
            for repo in page:
                yield f"repo:{repo['id']}", repo, "starred"


def sync(payload):
    config = payload.get("config") or {}
    username = str(config.get("username") or "").strip().lstrip("@")
    token = str(config.get("token") or "").strip()
    if not username and not token:
        raise InputError("username is empty: enter your GitHub user name")
    if username and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", username):
        raise InputError(f"username is not a GitHub user name: {username}")

    client = Client(token)
    if token:
        me, _ = client.get(f"{client.base}/user")
        login = me.get("login", "")
        if username and login.lower() != username.lower():
            raise InputError(f"the token belongs to {login}, not {username}")
        username = login

    cursor = load_cursor(payload.get("cursor"))
    known = cursor.get("repos", {})
    seen = {}
    written = 0
    try:
        for rid, repo, relation in list_repos(client, username, bool(token), flag(config, "include_starred", True)):
            # Stars and `updated_at` change whenever someone stars a popular repository; only what
            # the record shows apart from the star count decides whether it is written again.
            stamp = json.dumps(
                [relation, repo.get("pushed_at"), repo.get("description"), repo.get("topics"),
                 repo.get("language"), repo.get("archived"), repo.get("private"), repo.get("full_name")],
                separators=(",", ":"),
            )
            if rid in seen:
                continue  # an owned repository the account also starred
            seen[rid] = stamp
            if known.get(rid) != stamp:
                emit({"upsert": "item", "external_id": rid, "fields": repo_record(repo, relation)})
                written += 1
    except RateLimited as limited:
        # Without the full list a deletion could be wrong: keep everything and try again later.
        log("warn", f"{limited}; repositories resume on the next sync")
        print(limited, file=sys.stderr)
        return 1

    removed = [rid for rid in known if rid not in seen]
    for rid in removed:
        emit({"delete": "item", "external_id": rid})

    since = cursor.get("issues_since")
    issues = 0
    if token and flag(config, "include_issues", True):
        params = {"filter": "all", "state": "all", "sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        try:
            for page in client.pages("/issues", params):
                for issue in page:
                    emit({"upsert": "item", "external_id": f"issue:{issue['id']}", "fields": issue_record(issue)})
                    issues += 1
                    since = max(since or "", issue.get("updated_at") or "")
        except RateLimited as limited:
            log("warn", f"{limited}; issues resume on the next sync")

    emit({"cursor": json.dumps({"v": CURSOR_VERSION, "repos": seen, "issues_since": since}, separators=(",", ":"))})
    log(
        "info",
        f"{username}: {len(seen)} repositories ({written} written, {len(removed)} removed), "
        f"{issues} issues and pull requests, {client.requests} requests",
    )
    return 0


# ── Detect ──────────────────────────────────────────────────────────────────


def gh_hosts_files():
    home = os.path.expanduser("~")
    paths = [os.path.join(home, ".config", "gh", "hosts.yml")]
    if os.environ.get("APPDATA"):
        paths.append(os.path.join(os.environ["APPDATA"], "GitHub CLI", "hosts.yml"))
    return paths


def detect():
    """Suggest the user name the GitHub CLI is signed in with; never read its token."""
    users = []
    for path in gh_hosts_files():
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        in_github = False
        for line in lines:
            if line and not line[0].isspace():
                in_github = line.rstrip(":").strip() == "github.com"
                continue
            match = re.match(r"\s+user:\s*['\"]?([A-Za-z0-9-]+)", line)
            if in_github and match and match.group(1) not in users:
                users.append(match.group(1))
    if users:
        emit({"suggest": {"username": users}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("contract") != 1:
            raise InputError("expected a contract 1 input document on stdin")
        if payload.get("mode") == "detect":
            detect()
            return 0
        return sync(payload)
    except InputError as error:
        print(error, file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"could not reach GitHub: {getattr(error, 'reason', error)}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"unexpected input: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
