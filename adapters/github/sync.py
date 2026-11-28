#!/usr/bin/env python3
"""GitHub adapter for WingDrive (contract 1).

Indexes a GitHub account: the repositories it owns, the ones it starred and,
with a token, the issues and pull requests it takes part in and their comments.
Every repository, issue and pull request is one record of the `item` model, told
apart by `kind`; each comment is a `comment` record that belongs to its item, and
the item's `comments` field holds their text so search finds it.

Repositories are listed in full on each sync (a few requests even for large
accounts) so unstarred or deleted ones disappear; the cursor keeps each
repository's `updated_at` so only changed ones are written again. Issues and
pull requests come incrementally through `/issues?since=`. An issue that changed
has its comments listed again, which is how edited and deleted comments are
noticed; the ids seen per issue are kept in the source's state folder. Comments
left over when the rate limit or the time budget runs out wait in the cursor.

Only the Python standard library is used.
"""

import email.message
import json
import os
import pathlib
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
# Listing comments takes one request per issue; stop after this long and continue next sync, so a
# first sync of a large account stays inside the runtime timeout.
COMMENT_SECONDS = 600
USER_AGENT = "WingDrive-adapter-github/1.1"


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


def recorded_responses(config):
    """Responses a test case recorded, or None to ask GitHub.

    The contract harness runs this adapter without a network fake, so its cases ship the API's
    answers in `fixtures/<case>/api/responses.json` and point `recorded_api` at that folder.
    Only folders inside this adapter's own `fixtures/` are accepted, so the setting can never make
    the adapter read anything else.
    """
    folder = str(config.get("recorded_api") or "").strip()
    if not folder:
        return None
    fixtures = (pathlib.Path(__file__).resolve().parent / "fixtures").resolve()
    path = pathlib.Path(folder).resolve()
    if fixtures not in path.parents:
        raise InputError("recorded_api must be a folder inside the adapter's fixtures")
    with open(path / "responses.json", encoding="utf-8") as handle:
        return json.load(handle)


class Client:
    def __init__(self, token, recorded=None):
        self.base = api_base()
        self.token = token
        self.requests = 0
        self.recorded = recorded

    def replay(self, url):
        """A recorded answer for `url`: (json, next page URL or None), or the HTTPError it got."""
        key = url[len(self.base):]
        if key not in self.recorded:
            raise ValueError(f"no recorded response for {key}")
        answer = self.recorded[key]
        status = answer.get("status", 200)
        if status != 200:
            headers = email.message.Message()
            for name, value in (answer.get("headers") or {}).items():
                headers[name] = value
            raise urllib.error.HTTPError(url, status, answer.get("reason", ""), headers, None)
        following = answer.get("next")
        return answer.get("body"), self.base + following if following else None

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
            if self.recorded is not None:
                return self.replay(url)
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


def comment_record(comment, parent):
    body = comment.get("body") or ""
    return {
        "item_id": parent,
        "body": body if len(body) <= MAX_BODY else body[:MAX_BODY],
        "author": (comment.get("user") or {}).get("login", ""),
        "url": comment.get("html_url", ""),
        "created": comment.get("created_at"),
        "updated": comment.get("updated_at"),
    }


def comments_text(comments):
    """The comments of an issue as one searchable text, oldest first."""
    parts = []
    for comment in comments:
        author = (comment.get("user") or {}).get("login", "")
        parts.append(f"{author}: {(comment.get('body') or '').strip()}")
    text = "\n\n".join(parts)
    return text if len(text) <= MAX_BODY else text[:MAX_BODY]


def repo_from_url(url):
    match = re.search(r"/repos/([^/]+/[^/]+)$", url)
    return match.group(1) if match else ""


# ── Sync ────────────────────────────────────────────────────────────────────


def flag(config, key, default):
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off")
    return bool(value)


def state_file(payload):
    data_dir = payload.get("data_dir")
    return pathlib.Path(data_dir) / "comments.json" if data_dir else None


def load_comment_ids(path):
    """{issue record id: [comment record ids]} from the last sync, or {} without a state folder."""
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_comment_ids(path, known):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(known, handle, separators=(",", ":"), sort_keys=True)
    os.replace(partial, path)


def sync_comments(client, queue, known):
    """List the comments of each queued issue; returns (comments written, issues left for later)."""
    deadline = time.monotonic() + COMMENT_SECONDS
    written = 0
    for index, (parent, repo, number) in enumerate(queue):
        if time.monotonic() > deadline:
            log("info", f"{len(queue) - index} issues wait for their comments until the next sync")
            return written, queue[index:]
        comments = []
        try:
            for page in client.pages(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}):
                comments.extend(page)
        except RateLimited as limited:
            log("warn", f"{limited}; comments resume on the next sync")
            return written, queue[index:]
        except urllib.error.HTTPError as error:
            if error.code not in (404, 410):
                raise
            # The repository went private, was deleted or the issue moved: its comments go too.
        ids = []
        for comment in comments:
            cid = f"comment:{comment['id']}"
            emit({"upsert": "comment", "external_id": cid, "fields": comment_record(comment, parent)})
            ids.append(cid)
            written += 1
        for cid in known.get(parent, []):
            if cid not in ids:
                emit({"delete": "comment", "external_id": cid})
        emit({"upsert": "item", "external_id": parent, "fields": {"comments": comments_text(comments)}})
        if ids:
            known[parent] = ids
        else:
            known.pop(parent, None)
    return written, []


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

    client = Client(token, recorded_responses(config))
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
    with_comments = token and flag(config, "include_issues", True) and flag(config, "include_comments", True)
    comment_ids = load_comment_ids(state_file(payload))
    queue = [entry for entry in cursor.get("comments_pending", []) if isinstance(entry, list) and len(entry) == 3]
    if token and flag(config, "include_issues", True):
        params = {"filter": "all", "state": "all", "sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        try:
            for page in client.pages("/issues", params):
                for issue in page:
                    record = issue_record(issue)
                    rid = f"issue:{issue['id']}"
                    emit({"upsert": "item", "external_id": rid, "fields": record})
                    issues += 1
                    # An issue whose comment count fell to 0 is listed too, to delete the old ones.
                    if with_comments and (issue.get("comments") or rid in comment_ids) and record["repo"]:
                        queue.append([rid, record["repo"], issue.get("number")])
                    since = max(since or "", issue.get("updated_at") or "")
        except RateLimited as limited:
            log("warn", f"{limited}; issues resume on the next sync")

    comments, pending = 0, []
    if with_comments:
        unique, listed = [], set()
        for entry in queue:
            if entry[0] not in listed:
                listed.add(entry[0])
                unique.append(entry)
        comments, pending = sync_comments(client, unique, comment_ids)
        save_comment_ids(state_file(payload), comment_ids)

    state = {"v": CURSOR_VERSION, "repos": seen, "issues_since": since}
    if pending:
        state["comments_pending"] = pending
    emit({"cursor": json.dumps(state, separators=(",", ":"))})
    log(
        "info",
        f"{username}: {len(seen)} repositories ({written} written, {len(removed)} removed), "
        f"{issues} issues and pull requests, {comments} comments, {client.requests} requests",
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
