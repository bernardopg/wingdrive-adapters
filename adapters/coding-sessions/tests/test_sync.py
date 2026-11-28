"""Tests for the coding agent sessions adapter. Run with `python -m unittest discover -s tests -t tests`."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ENTRY = HERE / "sync.py"
sys.path.insert(0, str(HERE))

import sync  # noqa: E402


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
        self.config = {"claude_dir": str(self.claude), "codex_dir": str(self.codex)}
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
        config = {"claude_dir": str(self.root / "gone"), "codex_dir": str(self.codex)}
        done, lines = run(config)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn({"log": "warn", "message": f"claude_dir: {self.root / 'gone'} is not a folder; skipping it"}, lines)

    def test_detect_suggests_the_default_folders_that_exist(self):
        home = self.root / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        done, lines = run({}, mode="detect", home=str(home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(lines, [{"suggest": {"claude_dir": [str(home / ".claude" / "projects")]}}])


if __name__ == "__main__":
    unittest.main()
