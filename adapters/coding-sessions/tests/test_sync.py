"""Tests for the coding agent sessions adapter. Run with `python -m unittest discover -s tests -t tests`."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ENTRY = HERE / "sync.py"
sys.path.insert(0, str(HERE))

import sync  # noqa: E402
from wingdrive_adapter import Adapter  # noqa: E402

sys.path.insert(0, str(HERE / "tests"))
import opencode_db  # noqa: E402


def run(config, cursor=None, mode=None, home=None):
    payload = {"contract": 1, "config": config, "cursor": cursor, "data_dir": None}
    if mode:
        payload["mode"] = mode
    env = {"PATH": os.environ.get("PATH", ""), "HOME": home or "/nonexistent-home"}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        env["USERPROFILE"] = env["HOME"]
    done = subprocess.run(
        [sys.executable, "-I", str(ENTRY)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        env=env,
    )
    lines = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    return done, lines


def sync_here(config, cursor=None, data_dir=None):
    """Run a sync in this process, so tests can watch what it reads."""
    out = io.StringIO()
    adapter = Adapter({"contract": 1, "config": config, "cursor": cursor,
                       "data_dir": str(data_dir) if data_dir else None}, out=out, environ={})
    sync.sync(adapter)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def cursor_of(lines):
    return lines[-1]["cursor"]


def kinds(lines, kind, model=None):
    return [line for line in lines if kind in line and (model is None or line[kind] == model)]


def write_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")


def claude_turn(kind, content, **more):
    item = {"type": kind, "timestamp": more.pop("timestamp", "2026-11-28T10:00:00Z"),
            "cwd": "/work/app", "message": {"role": kind, "content": content}}
    item.update(more)
    return item


class Redaction(unittest.TestCase):
    def test_known_secret_shapes_are_replaced(self):
        cases = [
            "sk-ant-api03-" + "a" * 40,
            "sk-proj-" + "b" * 40,
            "ghp_" + "c" * 36,
            "github_pat_" + "d" * 40,
            "xoxb-1234567890-abcdefghij",
            "AKIA" + "E" * 16,
            "AIza" + "f" * 35,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl",
        ]
        for secret in cases:
            self.assertEqual(sync.redact(f"use {secret} now"), "use [secret] now", secret)

    def test_assignments_keep_their_name(self):
        self.assertEqual(sync.redact("password = hunter22!"), "password = [secret]")
        self.assertEqual(sync.redact('API_KEY="abcdef123456"'), 'API_KEY="[secret]"')
        self.assertEqual(sync.redact("the token is short"), "the token is short")

    def test_private_keys_are_replaced_whole(self):
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----"
        self.assertEqual(sync.redact(f"key:\n{key}\nend"), "key:\n[secret]\nend")


class Sync(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.config = {"claude_dir": str(self.claude), "codex_dir": str(self.codex),
                       "opencode_db": str(self.root / "no-opencode.db")}
        write_jsonl(self.claude / "-work-app" / "s1.jsonl", [
            claude_turn("user", "First question about the app"),
            claude_turn("assistant", [{"type": "text", "text": "An answer"}],
                        timestamp="2026-11-28T11:00:00Z"),
        ])

    def upserts(self, lines):
        return [line for line in lines if "upsert" in line]

    def test_title_falls_back_to_the_first_prompt(self):
        done, lines = run(self.config)
        self.assertEqual(done.returncode, 0, done.stderr)
        record = self.upserts(lines)[0]
        self.assertEqual(record["external_id"], "claude:s1")
        self.assertEqual(record["fields"]["title"], "First question about the app")
        self.assertEqual(record["fields"]["started"], "2026-11-28T10:00:00Z")
        self.assertEqual(record["fields"]["updated"], "2026-11-28T11:00:00Z")
        self.assertIn("cursor", lines[-1])

    def test_unchanged_files_are_not_read_again_and_removed_ones_are_deleted(self):
        _, lines = run(self.config)
        cursor = lines[-1]["cursor"]
        _, lines = run(self.config, cursor)
        self.assertEqual(self.upserts(lines), [])

        write_jsonl(self.claude / "-work-app" / "s1.jsonl", [
            claude_turn("user", "First question about the app"),
            claude_turn("user", "And a second one"),
        ])
        _, lines = run(self.config, cursor)
        self.assertEqual([r["fields"]["prompts"] for r in self.upserts(lines)], [2])
        cursor = lines[-1]["cursor"]

        (self.claude / "-work-app" / "s1.jsonl").unlink()
        _, lines = run(self.config, cursor)
        self.assertEqual([line for line in lines if "delete" in line],
                         [{"delete": "session", "external_id": "claude:s1"}])

    def test_malformed_lines_and_tool_only_transcripts_are_skipped(self):
        path = self.claude / "-work-app" / "s2.jsonl"
        path.write_text("{broken\n[1, 2]\n" + json.dumps(
            claude_turn("user", [{"type": "tool_result", "content": "only output"}])) + "\n")
        done, lines = run(self.config)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([r["external_id"] for r in self.upserts(lines)], ["claude:s1"])

    def test_injected_text_and_subagents_are_left_out(self):
        write_jsonl(self.claude / "-work-app" / "s1.jsonl", [
            claude_turn("user", "<command-name>/clear</command-name>"),
            claude_turn("user", "Real question"),
            claude_turn("assistant", [{"type": "text", "text": "side work"}], isSidechain=True),
            claude_turn("assistant", [{"type": "thinking", "thinking": "hidden"},
                                      {"type": "text", "text": "Main answer"}]),
        ])
        _, lines = run(self.config)
        body = self.upserts(lines)[0]["fields"]["body"]
        self.assertEqual(body, "## You\nReal question\n\n## Agent\nMain answer")

    def test_a_missing_chosen_folder_is_a_warning_not_a_failure(self):
        config = dict(self.config, claude_dir=str(self.root / "gone"))
        done, lines = run(config)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn({"log": "warn", "message": f"claude_dir: {self.root / 'gone'} is not a folder; skipping it"}, lines)

    def test_detect_suggests_the_default_folders_that_exist(self):
        home = self.root / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        done, lines = run({}, mode="detect", home=str(home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(lines, [{"suggest": {"claude_dir": [str(home / ".claude" / "projects")]}}])

        opencode = home / ".local" / "share" / "opencode"
        opencode.mkdir(parents=True)
        opencode_db.build_v2(opencode / "opencode.db")
        _, lines = run({}, mode="detect", home=str(home))
        self.assertIn({"suggest": {"opencode_db": [str(opencode / "opencode.db")]}}, lines)


class Incremental(unittest.TestCase):
    """Appended transcripts are read from where the last sync stopped."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.claude = self.root / "claude"
        self.state = self.root / "state"
        self.config = {"claude_dir": str(self.claude), "codex_dir": str(self.root / "codex"),
                       "opencode_db": str(self.root / "no-opencode.db")}
        self.path = self.claude / "-work-app" / "s1.jsonl"
        write_jsonl(self.path, [
            claude_turn("user", "First question"),
            claude_turn("assistant", [{"type": "text", "text": "First answer"},
                                      {"type": "tool_use", "name": "Edit",
                                       "input": {"file_path": "/work/app/a.py"}}]),
        ])
        self.offsets = []
        original = sync.read_from

        def watched(path, offset, handle_item):
            self.offsets.append(offset)
            return original(path, offset, handle_item)

        sync.read_from = watched
        self.addCleanup(setattr, sync, "read_from", original)

    def append(self, *items, raw=""):
        with open(self.path, "a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item) + "\n")
            handle.write(raw)

    def session(self, lines):
        records = kinds(lines, "upsert", "session")
        self.assertEqual(len(records), 1, lines)
        return records[0]["fields"]

    def test_appended_lines_are_read_from_the_last_offset(self):
        lines = sync_here(self.config, data_dir=self.state)
        size = self.path.stat().st_size
        self.append(claude_turn("user", "Second question", timestamp="2026-11-28T12:00:00Z"))
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(self.offsets, [0, size])
        fields = self.session(lines)
        self.assertEqual(fields["prompts"], 2)
        self.assertEqual(fields["updated"], "2026-11-28T12:00:00Z")
        self.assertEqual(fields["body"], "## You\nFirst question\n\n## Agent\nFirst answer\n\n## You\nSecond question")
        self.assertEqual(fields["files"], "/work/app/a.py")

    def test_a_truncated_or_rewritten_file_is_read_again_whole(self):
        lines = sync_here(self.config, data_dir=self.state)
        write_jsonl(self.path, [claude_turn("user", "Only this")])
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(self.offsets, [0, 0])
        self.assertEqual(self.session(lines)["body"], "## You\nOnly this")

        # Same start, longer file, but the bytes before the old offset changed.
        write_jsonl(self.path, [claude_turn("user", "Only that"), claude_turn("user", "and more text")])
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(self.offsets[-1], 0)
        self.assertEqual(self.session(lines)["prompts"], 2)

    def test_a_line_still_being_written_is_read_once_it_is_whole(self):
        lines = sync_here(self.config, data_dir=self.state)
        line = json.dumps(claude_turn("user", "Half written"))
        self.append(raw=line[:20])
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(self.session(lines)["prompts"], 1)
        self.append(raw=line[20:] + "\n")
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(self.session(lines)["prompts"], 2)
        self.assertEqual(self.offsets[1], self.offsets[2])

    def test_without_a_state_folder_changed_files_are_read_whole(self):
        lines = sync_here(self.config)
        self.append(claude_turn("user", "Second question"))
        lines = sync_here(self.config, cursor_of(lines))
        self.assertEqual(self.offsets, [0, 0])
        self.assertEqual(self.session(lines)["prompts"], 2)

    def test_a_cursor_from_0_1_0_reads_everything_again_and_deletes_what_is_gone(self):
        old = json.dumps({"v": 1, "files": {
            "claude:-work-app/s1.jsonl": [self.path.stat().st_size, self.path.stat().st_mtime_ns, "claude:s1"],
            "claude:-work-app/gone.jsonl": [10, 1, "claude:gone"],
        }})
        lines = sync_here(self.config, old, self.state)
        self.assertIn("summary", self.session(lines))
        self.assertEqual(kinds(lines, "delete"), [{"delete": "session", "external_id": "claude:gone"}])
        self.assertIn('"v":2', cursor_of(lines))

    def test_large_sessions_keep_their_full_text_and_a_short_summary(self):
        long_answer = "word " * 100_000
        self.append(claude_turn("assistant", [{"type": "text", "text": long_answer}]))
        fields = self.session(sync_here(self.config, data_dir=self.state))
        self.assertGreater(len(fields["body"]), 500_000)
        self.assertTrue(fields["body"].endswith(long_answer.strip()))
        first, last = fields["summary"].split("\n\n")
        self.assertEqual(first, "First question")
        self.assertLessEqual(len(last), sync.SUMMARY_PART)
        self.assertTrue(last.endswith("…"))


class Links(unittest.TestCase):
    """Sessions link to tool and file records, which go away with the last session using them."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.claude = self.root / "claude"
        self.state = self.root / "state"
        self.config = {"claude_dir": str(self.claude), "codex_dir": str(self.root / "codex"),
                       "opencode_db": str(self.root / "no-opencode.db")}

    def write(self, name, *files, tool="Edit", prompt="Change things"):
        write_jsonl(self.claude / "-work-app" / f"{name}.jsonl", [
            claude_turn("user", prompt),
            claude_turn("assistant", [{"type": "tool_use", "name": tool, "input": {"file_path": path}}
                                      for path in files] + [{"type": "text", "text": "Done"}]),
        ])

    def test_links_follow_the_sessions(self):
        self.write("s1", "/work/app/a.py", "/work/app/b.py")
        self.write("s2", "/work/app/a.py")
        lines = sync_here(self.config, data_dir=self.state)
        self.assertEqual(sorted(r["external_id"] for r in kinds(lines, "upsert", "file")),
                         ["/work/app/a.py", "/work/app/b.py"])
        self.assertEqual(len(kinds(lines, "upsert", "tool")), 1)
        self.assertEqual(len(kinds(lines, "link")), 5)

        self.write("s1", "/work/app/a.py", prompt="Change fewer things")
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(kinds(lines, "upsert", "file"), [])
        self.assertEqual(kinds(lines, "link"), [])
        self.assertEqual(kinds(lines, "unlink"),
                         [{"unlink": "session", "id": "claude:s1", "to": "file", "to_id": "/work/app/b.py"}])
        self.assertEqual(kinds(lines, "delete"), [{"delete": "file", "external_id": "/work/app/b.py"}])

        (self.claude / "-work-app" / "s1.jsonl").unlink()
        (self.claude / "-work-app" / "s2.jsonl").unlink()
        lines = sync_here(self.config, cursor_of(lines), self.state)
        self.assertEqual(sorted((r["delete"], r["external_id"]) for r in kinds(lines, "delete")),
                         [("file", "/work/app/a.py"), ("session", "claude:s1"), ("session", "claude:s2"),
                          ("tool", "Edit")])

    def test_relative_paths_join_the_session_folder(self):
        self.assertEqual(sync.absolute("src/a.py", "/work/app"), "/work/app/src/a.py")
        self.assertEqual(sync.absolute("src/a.py", "C:\\work\\app"), "C:\\work\\app\\src\\a.py")
        self.assertEqual(sync.absolute("/abs/a.py", "/work/app"), "/abs/a.py")
        self.assertEqual(sync.absolute("D:\\x.py", "/work/app"), "D:\\x.py")


class OpenCode(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.db = self.root / "opencode.db"
        self.config = {"claude_dir": str(self.root / "claude"), "codex_dir": str(self.root / "codex"),
                       "opencode_db": str(self.db)}

    def test_reads_top_level_sessions_and_leaves_out_injected_text(self):
        opencode_db.build_v2(self.db)
        lines = sync_here(self.config)
        records = kinds(lines, "upsert", "session")
        self.assertEqual([r["external_id"] for r in records], ["opencode:ses_main"])
        fields = records[0]["fields"]
        self.assertNotIn("Text OpenCode adds itself", fields["body"])
        self.assertNotIn("hidden reasoning", fields["body"])
        self.assertNotIn("output the adapter leaves out", fields["body"])
        self.assertEqual(fields["tools"], "apply_patch, edit, read")

    def test_a_new_message_reads_the_session_again_and_a_removed_one_is_deleted(self):
        opencode_db.build_v2(self.db)
        cursor = cursor_of(sync_here(self.config))
        self.assertEqual(kinds(sync_here(self.config, cursor), "upsert"), [])

        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO session_message VALUES ('msg99', 'ses_main', 'user', 99, ?, ?, ?)",
                     (opencode_db.T0 + 99_000, opencode_db.T0 + 99_000, json.dumps({"text": "One more thing"})))
        conn.commit()
        lines = sync_here(self.config, cursor)
        self.assertEqual(kinds(lines, "upsert", "session")[0]["fields"]["prompts"], 2)
        cursor = cursor_of(lines)

        conn.execute("DELETE FROM session_message WHERE session_id = 'ses_main'")
        conn.execute("DELETE FROM session_v2 WHERE id = 'ses_main'")
        conn.commit()
        conn.close()
        lines = sync_here(self.config, cursor)
        self.assertEqual(kinds(lines, "delete", "session"),
                         [{"delete": "session", "external_id": "opencode:ses_main"}])

    def test_the_schema_before_1_3_is_read_too(self):
        opencode_db.build_v1(self.db)
        records = kinds(sync_here(self.config), "upsert", "session")
        self.assertEqual(len(records), 1)
        fields = records[0]["fields"]
        self.assertEqual(fields["body"], "## You\nThe feed is empty\n\n## Agent\nRebuilt the feed.")
        self.assertEqual(fields["files"], "/home/ana/blog/feed.xml")
        self.assertEqual(fields["started"], "2026-11-28T10:00:00.000Z")

    def test_an_unreadable_database_keeps_its_sessions(self):
        opencode_db.build_v2(self.db)
        cursor = cursor_of(sync_here(self.config))
        self.db.write_bytes(b"not a database at all" * 100)
        lines = sync_here(self.config, cursor)
        self.assertEqual(kinds(lines, "delete"), [])
        warnings = [line["message"] for line in kinds(lines, "log") if line["log"] == "warn"]
        self.assertTrue(any(m.startswith("opencode_db: cannot read") for m in warnings), warnings)
        self.assertEqual(cursor_of(lines), cursor)

    def test_a_database_in_wal_mode_is_read_while_open_elsewhere(self):
        opencode_db.build_v2(self.db)
        writer = sqlite3.connect(str(self.db))
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO session_message VALUES ('msg99', 'ses_main', 'user', 99, 1, 1, ?)",
                       (json.dumps({"text": "Only in the WAL"}),))
        writer.commit()
        fields = kinds(sync_here(self.config), "upsert", "session")[0]["fields"]
        self.assertIn("Only in the WAL", fields["body"])

    def test_the_fixture_database_matches_its_builder(self):
        built = self.root / "built.db"
        opencode_db.build_v2(built)
        self.assertEqual(opencode_db.dump(opencode_db.FIXTURE), opencode_db.dump(built))


if __name__ == "__main__":
    unittest.main()
