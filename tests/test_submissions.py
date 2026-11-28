"""A good submission and the bad ones Wingbot must stop, run through the same checks as the CI.

The good submission is the Python template. With `WING_ADAPTER_TEST` set (the registry CI sets it
to the `wing-adapter-test` it builds), the template's own tests and the contract harness run too;
without it, only the harness is reported missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import wingreg  # noqa: E402
from wingreg import checks  # noqa: E402

TEMPLATE = ROOT / "templates" / "python"
HARNESS = bool(os.environ.get("WING_ADAPTER_TEST"))


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def submission(root: Path) -> Path:
    """A registry with the template as its only adapter, committed."""
    shutil.copytree(TEMPLATE, root / "adapters" / "text-notes", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "registry").mkdir()
    entry = {"id": "text-notes", "trust": "official", "source": {"type": "local", "path": "adapters/text-notes"}}
    (root / "registry" / "text-notes.json").write_text(json.dumps(entry))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    return root


def missing_harness(problems: list[str]) -> list[str]:
    return [p for p in problems if not p.startswith("the contract harness was not found")]


class Submissions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = submission(self.tmp / "reg")
        self.adapter = self.root / "adapters" / "text-notes"

    def validate(self) -> list[str]:
        problems = wingreg.validate(self.root, None, run_code=True)
        return problems if HARNESS else missing_harness(problems)

    def test_good_submission_passes(self) -> None:
        self.assertEqual(self.validate(), [])
        _, _, results = wingreg.check(self.root, None, self.tmp / "work", run_code=True)
        names = [row.name for row in results["text-notes"]]
        self.assertEqual(names[:3], ["Structure", "Code scan", "Adapter tests"])
        if HARNESS:
            self.assertEqual(names[3], "Contract harness")

    def test_bad_schema_fails(self) -> None:
        manifest = self.adapter / "adapter.toml"
        manifest.write_text(manifest.read_text().replace('version = "0.1.0"', 'version = "one"'))
        self.assertTrue(any("adapter.version" in p for p in self.validate()))

    def test_failing_tests_fail(self) -> None:
        (self.adapter / "tests" / "test_broken.py").write_text(
            "import unittest\n\nclass Broken(unittest.TestCase):\n    def test_it(self):\n        self.assertEqual(1, 2)\n"
        )
        problems = self.validate()
        self.assertTrue(any("adapter tests failed" in p for p in problems), problems)

    def test_undeclared_network_use_fails(self) -> None:
        sync = self.adapter / "sync.py"
        sync.write_text(
            sync.read_text().replace(
                "import sys\n", "import sys\nimport urllib.request\n\nTELEMETRY = 'https://collect.example.net/v1'\n", 1
            )
        )
        problems = self.validate()
        self.assertTrue(any("contacts collect.example.net" in p for p in problems), problems)
        self.assertTrue(any("imports urllib.request but permissions.network declares no host" in p for p in problems))

    def test_unsigned_update_fails(self) -> None:
        """An update whose tag no longer points at the pinned commit is refused before any code runs."""
        commit = "a" * 40
        entry = {
            "id": "text-notes",
            "trust": "community",
            "source": {"type": "git", "repository": "https://github.com/someone/text-notes", "tag": "v0.2.0", "commit": commit},
        }
        (self.root / "registry" / "text-notes.json").write_text(json.dumps(entry))
        moved = f"{'b' * 40}\trefs/tags/v0.2.0\n"
        with mock.patch.object(wingreg, "_run", return_value=moved) as run:
            problems = self.validate()
        self.assertTrue(any(f"does not point at {commit}" in p for p in problems), problems)
        self.assertEqual(run.call_count, 1, "nothing is fetched once the tag check fails")


class CodeScan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def scan(self, code: str, permissions: dict | None = None, config: list | None = None) -> list[str]:
        (self.tmp / "sync.py").write_text(code)
        manifest = {"adapter": {"permissions": permissions or {}, "config": config or []}}
        return checks.scan_code(self.tmp, manifest, set())

    def test_declared_hosts_and_loopback_pass(self) -> None:
        code = "import urllib.request\nAPI = 'https://api.github.com'\nLOCAL = 'http://127.0.0.1:8000'\n"
        self.assertEqual(self.scan(code, {"network": ["api.github.com"]}), [])
        self.assertEqual(self.scan("URL = 'https://eu.api.example.org/x'\n", {"network": ["*.example.org"]}), [])

    def test_docstrings_do_not_count_as_hosts(self) -> None:
        self.assertEqual(self.scan('"""See https://docs.example.org for the format."""\n'), [])

    def test_dynamic_code_is_refused(self) -> None:
        code = "import importlib, pickle\nexec(data)\neval('1')\nimportlib.import_module(name)\npickle.loads(b)\n"
        found = self.scan(code)
        for name in ("exec", "eval", "importlib.import_module", "pickle.loads"):
            self.assertTrue(any(f"({name})" in p for p in found), (name, found))

    def test_programs_must_be_declared_tools(self) -> None:
        code = "import subprocess\nfrom os import system\nsubprocess.run(['git', 'log'])\nsystem('rm -rf ~')\n"
        self.assertEqual(len(self.scan(code)), 2)
        found = self.scan(code, {"tools": ["git"]})
        self.assertEqual(found, ["sync.py:4 starts rm, which permissions.tools does not declare"])

    def test_packages_outside_the_standard_library_are_refused(self) -> None:
        (self.tmp / "helper.py").write_text("")
        found = self.scan("import requests\nimport json\nfrom helper import x\n")
        self.assertTrue(any("imports requests, which is not in the Python standard library" in p for p in found))
        self.assertTrue(any("permissions.network declares no host" in p for p in found))
        self.assertFalse(any("helper" in p or "json" in p for p in found), found)

    def test_home_paths_must_be_declared(self) -> None:
        code = "A = '~/.config/app/settings.json'\nB = '~/.ssh/id_ed25519'\n"
        found = self.scan(code, {"read": ["~/.config/app"]})
        self.assertEqual(found, ["sync.py:2 reads ~/.ssh/id_ed25519, which permissions.read does not declare"])

    def test_secret_fields_and_permissions_match(self) -> None:
        config = [{"key": "token", "secret": True}, {"key": "api_key", "type": "string"}]
        found = self.scan("", {"secrets": ["password"]}, config)
        self.assertIn("config field token is secret but permissions.secrets does not list it", found)
        self.assertIn("permissions.secrets lists password, which is not a secret config field", found)
        self.assertIn("config field api_key looks like a secret: mark it `secret = true`", found)

    def test_committed_credentials_are_found_but_fake_ones_pass(self) -> None:
        real = "gh" + "p_" + "Zq8RkT2vLm9XwP4nYc7Hb1Js6Fd3Ga5Ue0Ki"
        (self.tmp / "notes.txt").write_text(f"token: {real}\n")
        self.assertEqual(len(checks.scan_secrets(self.tmp)), 1)
        (self.tmp / "notes.txt").write_text("token: ghp_" + "c" * 36 + "\nkey: AKIAIOSFODNN7EXAMPLE\n")
        self.assertEqual(checks.scan_secrets(self.tmp), [])


if __name__ == "__main__":
    unittest.main()


class FolderCheck(unittest.TestCase):
    """`wingreg check <folder>`, which `wing adapter validate` runs on an adapter outside the registry."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.adapter = self.tmp / "my-adapter"
        shutil.copytree(TEMPLATE, self.adapter, ignore=shutil.ignore_patterns("__pycache__"))

    def problems(self, folder: Path, run_code: bool = True) -> list[str]:
        _, problems, _ = wingreg.check_folder(folder, run_code=run_code)
        return problems if HARNESS else missing_harness(problems)

    def test_the_template_passes_as_it_is(self) -> None:
        self.assertEqual(self.problems(self.adapter), [])

    def test_a_community_folder_needs_a_license(self) -> None:
        (self.adapter / "LICENSE").unlink()
        self.assertEqual(self.problems(self.adapter), ["text-notes: LICENSE file is missing"])
        (self.adapter / "LICENSE").write_text("Apache License 2.0\n")
        self.assertEqual(self.problems(self.adapter), [])
        _, _, results = wingreg.check_folder(self.adapter, run_code=True)
        self.assertEqual([row.name for row in results["text-notes"]][:3], ["Structure", "Code scan", "Adapter tests"])

    def test_the_same_code_scan_runs(self) -> None:
        sync = self.adapter / "sync.py"
        sync.write_text(sync.read_text() + "\nimport urllib.request\nURL = 'https://tracker.example.net/x'\n")
        problems = self.problems(self.adapter, run_code=False)
        self.assertTrue(any("tracker.example.net" in p for p in problems), problems)

    def test_official_adapters_share_the_registry_license(self) -> None:
        self.assertEqual(self.problems(ROOT / "adapters" / "obsidian", run_code=False), [])

    def test_a_folder_without_a_manifest_is_an_error(self) -> None:
        with self.assertRaises(wingreg.RegistryError):
            wingreg.check_folder(self.tmp)

    def test_the_wing_cli_is_called_with_its_subcommand(self) -> None:
        with mock.patch.dict(os.environ, {"WING_ADAPTER_TEST": "/opt/bin/wing"}):
            self.assertEqual(checks.find_harness(), ["/opt/bin/wing", "adapter", "test"])
        with mock.patch.dict(os.environ, {"WING_ADAPTER_TEST": "/opt/bin/wing-adapter-test"}):
            self.assertEqual(checks.find_harness(), ["/opt/bin/wing-adapter-test"])
