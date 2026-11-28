"""Tests for the Python adapter kit. Run with `python -m unittest discover -s kit/python/tests`."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))

from wingdrive_adapter import Adapter, InputError, config_home, data_home  # noqa: E402


def adapter(config=None, cursor=None, mode=None, data_dir=None, environ=None):
    payload = {"contract": 1, "config": config or {}, "cursor": cursor, "data_dir": data_dir}
    if mode:
        payload["mode"] = mode
    out = io.StringIO()
    return Adapter(payload, out=out, environ=environ or {}), out


def lines(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


class InputTests(unittest.TestCase):
    def test_rejects_other_contracts(self):
        with self.assertRaisesRegex(InputError, "contract 1"):
            Adapter({"contract": 2, "config": {}})

    def test_reads_config_values(self):
        run, _ = adapter({"name": "  vault ", "empty": " ", "count": "50", "tags": "a, b,,c", "on": "yes"})
        self.assertEqual(run.get("name"), "vault")
        self.assertIsNone(run.get("empty"))
        self.assertEqual(run.get("missing", "x"), "x")
        self.assertEqual(run.number("count"), 50)
        self.assertEqual(run.items("tags"), ["a", "b", "c"])
        self.assertTrue(run.flag("on"))
        self.assertFalse(run.flag("missing"))
        self.assertTrue(run.flag("missing", default=True))
        with self.assertRaisesRegex(InputError, "empty is required"):
            run.require("empty")

    def test_rejects_a_number_that_is_not_one(self):
        run, _ = adapter({"count": "many"})
        with self.assertRaisesRegex(InputError, "count must be a whole number"):
            run.number("count")

    def test_paths_expand_home_and_check_the_kind(self):
        with tempfile.TemporaryDirectory() as folder:
            run, _ = adapter({"folder": folder, "file": str(Path(folder) / "missing.txt")})
            self.assertEqual(run.path("folder"), Path(folder))
            with self.assertRaisesRegex(InputError, "is not a file"):
                run.path("file", must_be="file")
            self.assertEqual(run.path("file", must_be=None).name, "missing.txt")
        run, _ = adapter({"folder": "~/somewhere"})
        self.assertEqual(run.path("folder", must_be=None), Path.home() / "somewhere")

    def test_detect_mode_and_data_dir(self):
        run, _ = adapter(mode="detect")
        self.assertTrue(run.detecting)
        self.assertIsNone(run.data_dir)
        run, _ = adapter(data_dir="/state")
        self.assertFalse(run.detecting)
        self.assertEqual(run.data_dir, Path("/state"))

    def test_confined_reads_the_isolation_variable(self):
        run, _ = adapter(environ={"WINGDRIVE_ADAPTER_ISOLATION": "bubblewrap"})
        self.assertTrue(run.confined)
        run, _ = adapter(environ={"WINGDRIVE_ADAPTER_ISOLATION": "none"})
        self.assertFalse(run.confined)


class CursorTests(unittest.TestCase):
    def test_round_trip(self):
        run, out = adapter()
        self.assertEqual(run.cursor(), {})
        run.save_cursor({"seen": {"a.txt": 3}}, version=2)
        saved = lines(out)[0]["cursor"]
        again, _ = adapter(cursor=saved)
        self.assertEqual(again.cursor(version=2), {"seen": {"a.txt": 3}})

    def test_another_version_or_garbage_starts_over(self):
        run, _ = adapter(cursor=json.dumps({"v": 1, "seen": {}}))
        self.assertEqual(run.cursor(version=2), {})
        for raw in ("not json", "[1, 2]", ""):
            run, _ = adapter(cursor=raw)
            self.assertEqual(run.cursor(), {})


class OutputTests(unittest.TestCase):
    def test_lines_match_the_contract(self):
        run, out = adapter()
        run.upsert("note", 7, {"title": "Ana"}, words=2)
        run.delete("note", "gone")
        run.link("note", "a", "note", "b")
        run.unlink("note", "a", "note", "c")
        run.info("done")
        self.assertEqual(
            lines(out),
            [
                {"upsert": "note", "external_id": "7", "fields": {"title": "Ana", "words": 2}},
                {"delete": "note", "external_id": "gone"},
                {"link": "note", "id": "a", "to": "note", "to_id": "b"},
                {"unlink": "note", "id": "a", "to": "note", "to_id": "c"},
                {"log": "info", "message": "done"},
            ],
        )

    def test_unknown_log_level_is_a_bug(self):
        run, _ = adapter()
        with self.assertRaises(ValueError):
            run.log("verbose", "x")

    def test_suggestions_drop_empties_and_repeats(self):
        run, out = adapter(mode="detect")
        run.suggest("vault", ["/a", "", "/b", "/a"])
        run.suggest("other", [])
        self.assertEqual(lines(out), [{"suggest": {"vault": ["/a", "/b"]}}])

    def test_secrets_never_reach_logs(self):
        run, out = adapter({"token": "ghp_abc123", "short": "ab"})
        self.assertEqual(run.secret("token"), "ghp_abc123")
        run.warn("request with ghp_abc123 failed")
        self.assertEqual(lines(out), [{"log": "warn", "message": "request with [secret] failed"}])
        self.assertIsNone(run.secret("missing", required=False))
        with self.assertRaisesRegex(InputError, "missing is required"):
            run.secret("missing")


class PlatformPathTests(unittest.TestCase):
    def test_each_platform(self):
        env = {"HOME": "/home/ana", "APPDATA": r"C:\Users\ana\AppData\Roaming",
               "LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}
        self.assertEqual(config_home(env, "linux"), Path("/home/ana/.config"))
        self.assertEqual(data_home(env, "linux"), Path("/home/ana/.local/share"))
        self.assertEqual(config_home({**env, "XDG_CONFIG_HOME": "/cfg"}, "linux"), Path("/cfg"))
        self.assertEqual(config_home(env, "darwin"), Path("/home/ana/Library/Application Support"))
        self.assertEqual(config_home(env, "win32"), Path(env["APPDATA"]))
        self.assertEqual(data_home(env, "win32"), Path(env["LOCALAPPDATA"]))


class RunTests(unittest.TestCase):
    """`run` as the daemon starts it: a script reading stdin in a fresh interpreter."""

    SCRIPT = """
import sys
sys.path.insert(0, {kit!r})
from wingdrive_adapter import InputError, run

def sync(adapter):
    token = adapter.secret("token")
    adapter.upsert("note", "a", title="é")
    if adapter.get("fail"):
        raise InputError("token " + token + " was refused")

def detect(adapter):
    adapter.suggest("folder", ["/x"])

run(sync, detect)
"""

    def run_script(self, payload):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "sync.py"
            script.write_text(self.SCRIPT.format(kit=str(KIT)), encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-I", str(script)],
                input=payload.encode("utf-8"),
                capture_output=True,
            )

    def test_a_sync_exits_zero(self):
        done = self.run_script(json.dumps({"contract": 1, "config": {"token": "t0k"}}))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.decode("utf-8")),
                         {"upsert": "note", "external_id": "a", "fields": {"title": "é"}})

    def test_detect_mode_calls_detect(self):
        done = self.run_script(json.dumps({"contract": 1, "mode": "detect", "config": {}}))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"suggest": {"folder": ["/x"]}})

    def test_input_errors_exit_two_without_the_secret(self):
        done = self.run_script(json.dumps({"contract": 1, "config": {"token": "t0k", "fail": "1"}}))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stderr.decode().strip(), "token [secret] was refused")

    def test_bad_stdin_exits_two(self):
        done = self.run_script("not json")
        self.assertEqual(done.returncode, 2)
        self.assertIn("stdin is not JSON", done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
