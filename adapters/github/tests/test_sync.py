"""Tests for the GitHub adapter: runs sync.py against a local fake of the GitHub API."""

import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

ENTRY = pathlib.Path(__file__).resolve().parent.parent / "sync.py"


def repo(rid, name, owner="ana", **extra):
    data = {
        "id": rid,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner},
        "description": f"{name} description",
        "topics": ["cli"],
        "language": "Python",
        "stargazers_count": 3,
        "private": False,
        "html_url": f"https://github.com/{owner}/{name}",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-02-01T00:00:00Z",
        "pushed_at": "2025-02-02T00:00:00Z",
    }
    data.update(extra)
    return data


def issue(iid, title, updated, pull=False, merged=False):
    data = {
        "id": iid,
        "number": iid % 1000,
        "title": title,
        "body": f"{title} body",
        "state": "closed" if merged else "open",
        "user": {"login": "bob"},
        "labels": [{"name": "bug"}],
        "html_url": f"https://github.com/ana/tool/issues/{iid}",
        "repository_url": "https://api.github.com/repos/ana/tool",
        "created_at": "2025-03-01T00:00:00Z",
        "updated_at": updated,
    }
    if pull:
        data["pull_request"] = {"merged_at": "2025-03-05T00:00:00Z" if merged else None}
    return data


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    """Serves `self.server.routes`: path -> list of pages (each a JSON body)."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        server = self.server
        server.calls.append((url.path, query))
        if server.rate_limited:
            self.send_response(403)
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset", "0")
            self.end_headers()
            return
        auth = self.headers.get("Authorization")
        if auth and auth != "Bearer good-token":
            self.send_response(401)
            self.end_headers()
            return
        pages = server.routes.get(url.path)
        if pages is None:
            self.send_response(404)
            self.end_headers()
            return
        if url.path == "/issues" and "since" in query:
            pages = [[i for i in page if i["updated_at"] >= query["since"]] for page in pages]
        page = int(query.get("page", "1"))
        body = json.dumps(pages[page - 1]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if page < len(pages):
            query["page"] = str(page + 1)
            link = f"http://127.0.0.1:{server.server_port}{url.path}?{urllib.parse.urlencode(query)}"
            self.send_header("Link", f'<{link}>; rel="next"')
        self.end_headers()
        self.wfile.write(body)


class Sync:
    def __init__(self, result):
        self.result = result
        self.ops = [json.loads(line) for line in result.stdout.splitlines()]
        self.upserts = {op["external_id"]: op["fields"] for op in self.ops if "upsert" in op}
        self.deletes = [op["external_id"] for op in self.ops if "delete" in op]
        cursors = [op["cursor"] for op in self.ops if "cursor" in op]
        self.cursor = cursors[-1] if cursors else None


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.server.routes = {}
        self.server.calls = []
        self.server.rate_limited = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.home = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.home.cleanup()

    def run_adapter(self, config=None, cursor=None, mode=None):
        payload = {"contract": 1, "config": config or {}, "cursor": cursor, "data_dir": None}
        if mode:
            payload["mode"] = mode
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.home.name,
            "USERPROFILE": self.home.name,
            "WINGDRIVE_GITHUB_API": f"http://127.0.0.1:{self.server.server_port}",
        }
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            env["APPDATA"] = os.path.join(self.home.name, "AppData")
        result = subprocess.run(
            [sys.executable, "-I", str(ENTRY)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        return Sync(result)

    def public_account(self):
        self.server.routes["/users/ana/repos"] = [[repo(1, "tool"), repo(2, "notes")], [repo(3, "site")]]
        self.server.routes["/users/ana/starred"] = [[repo(10, "lib", owner="carol"), repo(1, "tool")]]

    def test_public_account_without_token(self):
        self.public_account()
        run = self.run_adapter({"username": "ana"})
        self.assertEqual(run.result.returncode, 0, run.result.stderr)
        self.assertEqual(set(run.upserts), {"repo:1", "repo:2", "repo:3", "repo:10"})
        tool = run.upserts["repo:1"]
        self.assertEqual(tool["kind"], "repository")
        self.assertEqual(tool["relation"], "owner")
        self.assertEqual(tool["repo"], "ana/tool")
        self.assertIn("Topics: cli", tool["body"])
        self.assertEqual(run.upserts["repo:10"]["relation"], "starred")
        # Two pages of owned repositories were followed; no authenticated endpoint was used.
        self.assertIn(("/users/ana/repos", {"type": "owner", "per_page": "100", "page": "2"}), self.server.calls)
        self.assertFalse(any(path in ("/user", "/issues") for path, _ in self.server.calls))

    def test_second_sync_writes_only_changes_and_drops_unstarred(self):
        self.public_account()
        first = self.run_adapter({"username": "ana"})
        self.server.routes["/users/ana/repos"] = [[repo(1, "tool", pushed_at="2025-06-01T00:00:00Z"), repo(2, "notes")], [repo(3, "site")]]
        self.server.routes["/users/ana/starred"] = [[repo(1, "tool")]]
        second = self.run_adapter({"username": "ana"}, first.cursor)
        self.assertEqual(second.result.returncode, 0, second.result.stderr)
        self.assertEqual(set(second.upserts), {"repo:1"})
        self.assertEqual(second.deletes, ["repo:10"])
        third = self.run_adapter({"username": "ana"}, second.cursor)
        self.assertEqual(third.upserts, {})
        self.assertEqual(third.deletes, [])

    def test_new_stars_alone_do_not_rewrite_a_repository(self):
        self.public_account()
        first = self.run_adapter({"username": "ana"})
        self.server.routes["/users/ana/starred"] = [[
            repo(10, "lib", owner="carol", stargazers_count=999, updated_at="2025-09-09T00:00:00Z"),
            repo(1, "tool"),
        ]]
        second = self.run_adapter({"username": "ana"}, first.cursor)
        self.assertEqual(second.upserts, {})

    def test_starred_can_be_left_out(self):
        self.public_account()
        run = self.run_adapter({"username": "ana", "include_starred": "false"})
        self.assertNotIn("repo:10", run.upserts)

    def test_token_adds_private_repos_issues_and_pull_requests(self):
        self.server.routes["/user"] = [{"login": "ana"}]
        self.server.routes["/user/repos"] = [[repo(1, "tool"), repo(4, "secret", private=True)]]
        self.server.routes["/user/starred"] = [[]]
        self.server.routes["/issues"] = [
            [issue(101, "Crash on start", "2025-03-02T00:00:00Z"), issue(102, "Add export", "2025-03-03T00:00:00Z", pull=True)],
            [issue(103, "Old fix", "2025-03-04T00:00:00Z", pull=True, merged=True)],
        ]
        run = self.run_adapter({"username": "ana", "token": "good-token"})
        self.assertEqual(run.result.returncode, 0, run.result.stderr)
        self.assertEqual(run.upserts["repo:4"]["state"], "private")
        self.assertEqual(run.upserts["issue:101"]["kind"], "issue")
        self.assertEqual(run.upserts["issue:101"]["repo"], "ana/tool")
        self.assertEqual(run.upserts["issue:101"]["labels"], "bug")
        self.assertEqual(run.upserts["issue:102"]["kind"], "pull_request")
        self.assertEqual(run.upserts["issue:103"]["state"], "merged")

        self.server.calls.clear()
        again = self.run_adapter({"username": "ana", "token": "good-token"}, run.cursor)
        since = [query.get("since") for path, query in self.server.calls if path == "/issues"]
        self.assertEqual(set(since), {"2025-03-04T00:00:00Z"})
        self.assertEqual(set(again.upserts), {"issue:103"})

    def test_token_of_another_user_is_refused(self):
        self.server.routes["/user"] = [{"login": "mallory"}]
        run = self.run_adapter({"username": "ana", "token": "good-token"})
        self.assertEqual(run.result.returncode, 2)
        self.assertIn("belongs to mallory", run.result.stderr)

    def test_bad_token(self):
        run = self.run_adapter({"username": "ana", "token": "expired"})
        self.assertEqual(run.result.returncode, 2)
        self.assertIn("refused the token", run.result.stderr)

    def test_unknown_user(self):
        run = self.run_adapter({"username": "nobody"})
        self.assertEqual(run.result.returncode, 2)
        self.assertIn("no user with that name", run.result.stderr)

    def test_recorded_answers_only_come_from_the_adapters_fixtures(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        pathlib.Path(outside, "responses.json").write_text("{}")
        run = self.run_adapter({"username": "ana", "recorded_api": outside})
        self.assertEqual(run.result.returncode, 2)
        self.assertIn("inside the adapter's fixtures", run.result.stderr)
        self.assertEqual(run.upserts, {})

    def test_recorded_answers_replay_a_case(self):
        case = ENTRY.parent / "fixtures" / "public-account" / "api"
        run = self.run_adapter({"username": "ana", "recorded_api": str(case)})
        self.assertEqual(run.result.returncode, 0, run.result.stderr)
        self.assertEqual(sorted(run.upserts), ["repo:1", "repo:10", "repo:2", "repo:3"])

    def test_rate_limit_keeps_everything(self):
        self.public_account()
        first = self.run_adapter({"username": "ana"})
        self.server.rate_limited = True
        limited = self.run_adapter({"username": "ana"}, first.cursor)
        self.assertEqual(limited.result.returncode, 1)
        self.assertIn("rate limit", limited.result.stderr)
        self.assertEqual(limited.deletes, [])
        self.assertIsNone(limited.cursor)

    def test_missing_and_invalid_username(self):
        self.assertEqual(self.run_adapter({}).result.returncode, 2)
        run = self.run_adapter({"username": "a b/../c"})
        self.assertEqual(run.result.returncode, 2)
        self.assertIn("not a GitHub user name", run.result.stderr)

    def test_detect_reads_the_cli_user_but_not_its_token(self):
        config = pathlib.Path(self.home.name) / ".config" / "gh"
        config.mkdir(parents=True)
        (config / "hosts.yml").write_text(
            "github.com:\n    users:\n        ana: {}\n    oauth_token: secret-value\n    user: ana\n"
            "example.com:\n    user: other\n"
        )
        run = self.run_adapter(mode="detect")
        self.assertEqual(run.result.returncode, 0, run.result.stderr)
        self.assertEqual(run.ops, [{"suggest": {"username": ["ana"]}}])
        self.assertNotIn("secret-value", run.result.stdout)

    def test_detect_without_the_cli_suggests_nothing(self):
        run = self.run_adapter(mode="detect")
        self.assertEqual(run.result.returncode, 0)
        self.assertEqual(run.ops, [])


if __name__ == "__main__":
    unittest.main()
