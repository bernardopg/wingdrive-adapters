"""Tests for tools/wingreg. Run with `uv run --with jsonschema python -m unittest discover -s tests`."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import wingreg  # noqa: E402

MANIFEST = """\
[adapter]
id = "sample"
name = "Sample"
summary = "Indexes a folder of sample notes so tests have something real to publish."
version = "1.0.0"
author = "WingDrive"
license = "Apache-2.0"
icon = "icon.svg"
contract = 1
platforms = ["linux", "macos"]
categories = ["notes"]

[adapter.runtime]
kind = "python3"
entry = "sync.py"

[adapter.permissions]
read = ["~/Notes"]

[data_type]
id = "sample"
name = "Sample note"

[models.note]
fields.title = "string"
fields.body = "text"

[search]
primary_model = "note"
title = "title"
preview = "body"
search_fields = ["title", "body"]
"""

README = "# Sample\n\n## Setup\n\nPick a folder.\n\n## Data\n\nNotes.\n\n## Privacy\n\nLocal only.\n"
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24"/></svg>'


def make_registry(root: Path, manifest: str = MANIFEST, entry: dict | None = None) -> Path:
    adapter = root / "adapters" / "sample"
    (adapter / "tests").mkdir(parents=True)
    (adapter / "adapter.toml").write_text(manifest)
    (adapter / "sync.py").write_text("print('{}')\n")
    (adapter / "README.md").write_text(README)
    (adapter / "icon.svg").write_text(ICON)
    (adapter / "tests" / "test_sample.py").write_text("")
    (root / "registry").mkdir()
    entry = entry or {"id": "sample", "trust": "official", "source": {"type": "local", "path": "adapters/sample"}}
    (root / "registry" / "sample.json").write_text(json.dumps(entry))
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    return root


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_valid_adapter_passes(self) -> None:
        root = make_registry(self.tmp / "reg")
        self.assertEqual(wingreg.validate(root), [])

    def test_schema_problems_name_the_field(self) -> None:
        root = make_registry(self.tmp / "reg", MANIFEST.replace('version = "1.0.0"', 'version = "one"'))
        problems = wingreg.validate(root)
        self.assertTrue(any("adapter.version" in p for p in problems), problems)

    def test_summary_longer_than_two_lines_fails(self) -> None:
        root = make_registry(self.tmp / "reg", MANIFEST.replace("Indexes a folder", "x" * 200))
        self.assertTrue(any("adapter.summary" in p for p in wingreg.validate(root)))

    def test_search_must_name_fields_of_the_primary_model(self) -> None:
        root = make_registry(self.tmp / "reg", MANIFEST.replace('preview = "body"', 'preview = "summary"'))
        problems = wingreg.validate(root)
        self.assertTrue(any("search.preview" in p and "summary" in p for p in problems), problems)

    def test_file_name_must_match_id(self) -> None:
        root = make_registry(self.tmp / "reg")
        (root / "registry" / "sample.json").rename(root / "registry" / "other.json")
        self.assertTrue(any("must be sample.json" in p for p in wingreg.validate(root)))

    def test_local_adapters_are_official(self) -> None:
        entry = {"id": "sample", "trust": "community", "source": {"type": "local", "path": "adapters/sample"}}
        root = make_registry(self.tmp / "reg", entry=entry)
        self.assertTrue(any("are official" in p for p in wingreg.validate(root)))

    def test_icon_must_be_square_and_script_free(self) -> None:
        root = make_registry(self.tmp / "reg")
        (root / "adapters/sample/icon.svg").write_text('<svg viewBox="0 0 24 12"><script>x</script></svg>')
        problems = wingreg.validate(root)
        self.assertTrue(any("must be square" in p for p in problems), problems)
        self.assertTrue(any("contains script" in p for p in problems), problems)

    def test_readme_sections_are_required(self) -> None:
        root = make_registry(self.tmp / "reg")
        (root / "adapters/sample/README.md").write_text("# Sample\n")
        self.assertTrue(any("'## Privacy'" in p for p in wingreg.validate(root)))

    def test_archives_are_reproducible(self) -> None:
        root = make_registry(self.tmp / "reg")
        (root / "adapters/sample/__pycache__").mkdir()
        (root / "adapters/sample/__pycache__/x.pyc").write_bytes(b"junk")
        first = wingreg.pack(root / "adapters/sample", "sample")
        second = wingreg.pack(root / "adapters/sample", "sample")
        self.assertEqual(wingreg.sha256(first), wingreg.sha256(second))
        self.assertNotIn(b"__pycache__", first)

    def test_index_is_built_and_matches_its_schema(self) -> None:
        root = make_registry(self.tmp / "reg")
        (root / "revoked.json").write_text(json.dumps([{"id": "sample", "versions": ["0.9.0"], "reason": "leaked a token"}]))
        out = self.tmp / "dist"
        index = wingreg.build_index(root, out, 7, "https://example.test/index-7/", self.tmp / "work")
        self.assertEqual(index["sequence"], 7)
        [item] = index["adapters"]
        self.assertEqual(item["permissions"], {"read": ["~/Notes"]})
        self.assertEqual(item["source"]["commit"], subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip())
        archive = (out / "sample-1.0.0.tar.gz").read_bytes()
        self.assertEqual(item["artifacts"][0]["sha256"], wingreg.sha256(archive))
        self.assertEqual(index["revoked"][0]["versions"], ["0.9.0"])
        self.assertEqual(json.loads((out / "index.json").read_text())["sequence"], 7)

    def test_changed_source_needs_a_version_bump(self) -> None:
        root = make_registry(self.tmp / "reg")
        previous = {"adapters": [{"id": "sample", "version": "1.0.0", "source_entry": {"type": "local", "path": "adapters/old"}}]}
        self.assertTrue(any("must increase" in p for p in wingreg.validate(root, previous)))
        previous["adapters"][0]["version"] = "2.0.0"
        self.assertTrue(any("lower than the published" in p for p in wingreg.validate(root, previous)))

    def test_compiled_adapters_wait_for_the_build_workflow(self) -> None:
        manifest = MANIFEST.replace('kind = "python3"\nentry = "sync.py"', 'kind = "binary"\nentry = "bin/sample"')
        root = make_registry(self.tmp / "reg", manifest)
        self.assertTrue(any("build.yml" in p for p in wingreg.validate(root)))


if __name__ == "__main__":
    unittest.main()
