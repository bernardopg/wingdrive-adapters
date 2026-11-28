"""Tests for the text notes template. Run with `python -m unittest discover -s tests -t tests`."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "sync.py"


def run(config, cursor=None):
    payload = {"contract": 1, "config": config, "cursor": cursor, "data_dir": None}
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
    )
    lines = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    return done, lines


def cursor_of(lines):
    return next(line["cursor"] for line in lines if "cursor" in line)


class TextNotesTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        (self.folder / "todo.txt").write_text("Call the bank\n", encoding="utf-8")
        (self.folder / "readme.md").write_text("# not a note\n", encoding="utf-8")

    def test_first_sync_prints_every_note_then_the_cursor(self):
        done, lines = run({"folder": str(self.folder)})
        self.assertEqual(done.returncode, 0, done.stderr)
        upserts = [line for line in lines if "upsert" in line]
        self.assertEqual([line["external_id"] for line in upserts], ["todo.txt"])
        self.assertEqual(upserts[0]["fields"]["words"], 3)
        self.assertIn("cursor", lines[-1])

    def test_later_syncs_print_only_changes_and_deletions(self):
        _, lines = run({"folder": str(self.folder)})
        cursor = cursor_of(lines)

        _, lines = run({"folder": str(self.folder)}, cursor)
        self.assertFalse([line for line in lines if "upsert" in line or "delete" in line])

        (self.folder / "new.txt").write_text("fresh", encoding="utf-8")
        (self.folder / "todo.txt").unlink()
        _, lines = run({"folder": str(self.folder)}, cursor)
        self.assertEqual([line["external_id"] for line in lines if "upsert" in line], ["new.txt"])
        self.assertEqual([line for line in lines if "delete" in line], [{"delete": "note", "external_id": "todo.txt"}])

    def test_a_missing_folder_is_an_input_error(self):
        done, lines = run({"folder": str(self.folder / "gone")})
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.decode().strip().endswith("is not a folder"))
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
