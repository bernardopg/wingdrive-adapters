"""Tests for tools/wingreg. Run with `uv run --with jsonschema python -m unittest discover -s tests`."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

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
    (adapter / "fixtures" / "empty").mkdir(parents=True)
    (adapter / "fixtures" / "empty" / "expected.json").write_text('{"counts": {"note": 0}}')
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

    def test_icon_with_event_handler_or_external_resource_is_refused(self) -> None:
        for bad in (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" onload="alert(1)"/>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><image href="https://x.test/a.png"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><foreignObject/></svg>',
        ):
            root = make_registry(self.tmp / f"reg{len(bad)}")
            (root / "adapters" / "sample" / "icon.svg").write_text(bad)
            self.assertTrue(any("icon" in p for p in wingreg.validate(root)), bad)

    def test_report_marks_new_permissions(self) -> None:
        root = make_registry(self.tmp / "reg", MANIFEST.replace('version = "1.0.0"', 'version = "1.1.0"').replace(
            'read = ["~/Notes"]', 'read = ["~/Notes", "~/Mail"]'))
        previous = {"adapters": [{"id": "sample", "version": "1.0.0", "permissions": {"read": ["~/Notes"]},
                                  "source_entry": {"type": "local", "path": "adapters/sample"}}]}
        text = wingreg.report(root, previous, self.tmp / "work")
        self.assertIn("<!-- wingbot-review -->", text)
        self.assertIn("Registry checks pass", text)
        self.assertIn("1.0.0 → 1.1.0", text)
        self.assertIn("**`~/Mail`** (new)", text)
        self.assertIn("`~/Notes`,", text)

    def test_announcement_covers_new_updated_and_revoked(self) -> None:
        def item(version, read):
            return {"id": "sample", "name": "Sample", "version": version, "summary": "S", "trust": "official",
                    "permissions": {"read": read}}
        previous = {"sequence": 4, "adapters": [item("1.0.0", ["~/Notes"])], "revoked": []}
        same = {"sequence": 5, "adapters": [item("1.0.0", ["~/Notes"])], "revoked": []}
        self.assertIsNone(wingreg.announcement(previous, same))
        current = {"sequence": 6, "adapters": [item("1.1.0", ["~/Notes"]), dict(item("0.1.0", []), id="other", name="Other")],
                   "revoked": [{"id": "sample", "versions": ["1.0.0"], "reason": "leaks data"}]}
        message = wingreg.announcement(previous, current)
        titles = [embed["title"] for embed in message["embeds"]]
        self.assertEqual(titles, ["Sample 1.1.0 (was 1.0.0)", "Other 0.1.0 is new in the store", "sample 1.0.0 revoked"])
        self.assertEqual(message["allowed_mentions"], {"parse": []})
        self.assertTrue(message["avatar_url"].endswith("/assets/wingbot.png"))
        self.assertIn("leaks data", message["embeds"][2]["description"])
        self.assertIn("none", message["embeds"][1]["fields"][2]["value"])

    def test_report_lists_problems(self) -> None:
        root = make_registry(self.tmp / "reg", MANIFEST.replace('version = "1.0.0"', 'version = "one"'))
        self.assertIn("Fails the registry checks", wingreg.report(root, None, self.tmp / "work"))

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

    def test_vendored_adapter_kit_must_be_a_release(self) -> None:
        kit = ROOT / "kit" / "python" / "wingdrive_adapter.py"
        root = make_registry(self.tmp / "reg")
        shutil.copy(kit, root / "adapters/sample/wingdrive_adapter.py")
        self.assertEqual(wingreg.validate(root), [])
        (root / "adapters/sample/wingdrive_adapter.py").write_text(kit.read_text() + "# changed\n")
        problems = wingreg.validate(root)
        self.assertTrue(any("wingdrive_adapter.py is not a released adapter kit" in p for p in problems), problems)

    def test_kit_and_template_copies_are_released(self) -> None:
        kit = ROOT / "kit" / "python" / "wingdrive_adapter.py"
        version = re.search(r'^KIT_VERSION = "([^"]+)"', kit.read_text(), re.MULTILINE).group(1)
        releases = json.loads((ROOT / "kit" / "python" / "releases.json").read_text())
        self.assertEqual(releases.get(version), hashlib.sha256(kit.read_bytes()).hexdigest(),
                         "a changed kit needs a new KIT_VERSION and its sha256 in releases.json")
        for template in sorted((ROOT / "templates").glob("*/" + wingreg.KIT_FILE)):
            self.assertEqual(template.read_bytes(), kit.read_bytes(), f"{template} is not the current kit")

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

    def test_changed_files_of_a_local_adapter_need_a_version_bump(self) -> None:
        root = make_registry(self.tmp / "reg")
        archive = wingreg.pack(root / "adapters/sample", "sample")
        entry = {"type": "local", "path": "adapters/sample"}
        same = {"adapters": [{"id": "sample", "version": "1.0.0", "source_entry": entry,
                              "artifacts": [{"target": "any", "sha256": wingreg.sha256(archive)}]}]}
        self.assertEqual(wingreg.validate(root, same), [])
        (root / "adapters/sample/sync.py").write_text("print('changed')\n")
        self.assertTrue(any("must increase" in p for p in wingreg.validate(root, same)))

    def test_index_has_png_icons_and_translations(self) -> None:
        manifest = MANIFEST.replace("[adapter.runtime]", '[adapter.i18n.pt]\nsummary = "Indexa uma pasta de notas de exemplo para os testes."\n\n[adapter.runtime]')
        root = make_registry(self.tmp / "reg", manifest)
        out = self.tmp / "dist"
        [item] = wingreg.build_index(root, out, 8, "https://example.test/index-8/", self.tmp / "work")["adapters"]
        png = (out / "sample-1.0.0.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(item["icon"]["png"]["sha256"], wingreg.sha256(png))
        self.assertEqual(item["i18n"]["pt"]["summary"], "Indexa uma pasta de notas de exemplo para os testes.")
        message = wingreg.announcement(None, {"sequence": 8, "adapters": [item], "revoked": []})
        self.assertEqual(message["embeds"][0]["thumbnail"]["url"], "https://example.test/index-8/sample-1.0.0.png")

    def test_translated_readme_keeps_the_three_sections(self) -> None:
        root = make_registry(self.tmp / "reg")
        (root / "adapters/sample/README.pt.md").write_text("# Exemplo\n\n## Configuração\n\nx\n\n## Dados\n\nx\n")
        self.assertTrue(any("README.pt.md needs a '## Privacidade'" in p for p in wingreg.validate(root)))
        (root / "adapters/sample/README.pt.md").write_text("# Exemplo\n\n## Configuração\n\n## Dados\n\n## Privacidade\n")
        self.assertEqual(wingreg.validate(root), [])

    def test_harness_cases_are_required(self) -> None:
        root = make_registry(self.tmp / "reg")
        shutil.rmtree(root / "adapters/sample/fixtures")
        self.assertTrue(any("fixtures/ has no case" in p for p in wingreg.validate(root)))

    def test_report_shows_checks_and_the_diff_since_the_last_pin(self) -> None:
        old, new = "1" * 40, "2" * 40
        source = {"type": "git", "repository": "https://github.com/someone/sample", "tag": "v1.1.0", "commit": new}
        adapter = wingreg.Adapter(
            entry={"id": "sample", "trust": "community", "source": source},
            manifest={"adapter": {"name": "Sample", "version": "1.1.0", "summary": "s", "runtime": {}}},
            directory=self.tmp,
        )
        previous = {"adapters": [{"id": "sample", "version": "1.0.0", "source_entry": {**source, "commit": old}}]}
        results = {"sample": [wingreg.checks.CheckResult("Structure", True), wingreg.checks.CheckResult("Contract harness", False, "FAIL basic")]}
        text = wingreg.render_report([adapter], ["sample: contract harness failed"], results, previous)
        self.assertIn(f"https://github.com/someone/sample/compare/{old}...{new}", text)
        self.assertIn("| Contract harness | **fail** |", text)
        self.assertIn("FAIL basic", text)

    def test_compiled_adapters_wait_for_the_build_workflow(self) -> None:
        manifest = MANIFEST.replace('kind = "python3"\nentry = "sync.py"', 'kind = "binary"\nentry = "bin/sample"')
        root = make_registry(self.tmp / "reg", manifest)
        self.assertTrue(any("build.yml" in p for p in wingreg.validate(root)))


if __name__ == "__main__":
    unittest.main()
