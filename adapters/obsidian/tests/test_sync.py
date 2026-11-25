"""Tests for the Obsidian adapter: runs sync.py as the daemon does and reads what it prints."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENTRY = pathlib.Path(__file__).resolve().parent.parent / "sync.py"


def run(vault, cursor=None, **config):
    payload = {"contract": 1, "config": {"vault_path": str(vault), **config}, "cursor": cursor, "data_dir": None}
    result = subprocess.run(
        [sys.executable, "-I", str(ENTRY)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    ops = [json.loads(line) for line in result.stdout.splitlines()]
    return result, ops


class Sync:
    """The operations of one run, grouped."""

    def __init__(self, ops):
        self.ops = ops
        self.upserts = {op["external_id"]: op["fields"] for op in ops if "upsert" in op}
        self.deletes = [op["external_id"] for op in ops if "delete" in op]
        self.links = {(op["id"], op["to_id"]) for op in ops if "link" in op}
        self.unlinks = {(op["id"], op["to_id"]) for op in ops if "unlink" in op}
        cursors = [op["cursor"] for op in ops if "cursor" in op]
        self.cursor = cursors[-1] if cursors else None

    def by_path(self, path):
        return next(f for f in self.upserts.values() if f["path"] == path)


def ids(cursor):
    return {state["p"]: nid for nid, state in json.loads(cursor)["notes"].items()}


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        self.vault.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text, mtime=None):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def sync(self, cursor=None, **config):
        result, ops = run(self.vault, cursor, **config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ops[-1]["log"], "info")
        self.assertIn("cursor", ops[-2])
        return Sync(ops)


class FirstSync(VaultTest):
    def test_reads_frontmatter_tags_aliases_and_dates(self):
        self.write(
            "Projects/Garden.md",
            """
            ---
            tags:
              - plants
              - "home/outdoor"
            aliases: [Backyard, Quintal]
            created: 2025-03-01 10:30
            ---
            # Garden

            Water the #tomatoes daily. Not a tag: #123 or `#code`.

            ```
            #notatag [[Nowhere]]
            ```
            """,
        )
        note = self.sync().by_path("Projects/Garden.md")
        self.assertEqual(note["title"], "Garden")
        self.assertEqual(note["folder"], "Projects")
        self.assertEqual(note["tags"], "home/outdoor, plants, tomatoes")
        self.assertEqual(note["aliases"], "Backyard, Quintal")
        self.assertEqual(note["created"], "2025-03-01T10:30:00+00:00")
        self.assertTrue(note["body"].startswith("# Garden"))
        self.assertNotIn("tags:", note["body"])

    def test_non_ascii_names_and_text(self):
        self.write("Café/Ação ✓.md", "---\ntags: [ação]\n---\nOlá #não_é [[Café/Outra]]\n")
        self.write("Café/Outra.md", "x\n")
        run_ = self.sync()
        note = run_.by_path("Café/Ação ✓.md")
        self.assertEqual(note["title"], "Ação ✓")
        self.assertEqual(note["tags"], "ação, não_é")
        nid = ids(run_.cursor)
        self.assertEqual(run_.links, {(nid["Café/Ação ✓.md"], nid["Café/Outra.md"])})

    def test_title_from_frontmatter_and_single_alias(self):
        self.write("a.md", "---\ntitle: Real title\naliases: My Alias\ntags: one two\n---\nbody\n")
        note = self.sync().by_path("a.md")
        self.assertEqual(note["title"], "Real title")
        self.assertEqual(note["aliases"], "My Alias")
        self.assertEqual(note["tags"], "one, two")

    def test_note_without_frontmatter(self):
        self.write("plain.md", "Just text with three words? No, seven words here.\n")
        note = self.sync().by_path("plain.md")
        self.assertEqual(note["tags"], "")
        self.assertEqual(note["word_count"], 9)

    def test_resolves_wikilinks_markdown_links_and_aliases(self):
        self.write("Index.md", "[[Garden]] [[Projects/Garden#Beds|beds]] ![[Recipes]] [[Quintal]] [[Missing]]\n"
                   "[see](Sub/Deep%20Note.md) ![[photo.png]] [web](https://x.org/a.md)\n")
        self.write("Projects/Garden.md", "---\naliases: [Quintal]\n---\nback to [[Index]]\n")
        self.write("Recipes.md", "food\n")
        self.write("Sub/Deep Note.md", "deep\n")
        run_ = self.sync()
        nid = ids(run_.cursor)
        self.assertEqual(
            run_.links,
            {
                (nid["Index.md"], nid["Projects/Garden.md"]),
                (nid["Index.md"], nid["Recipes.md"]),
                (nid["Index.md"], nid["Sub/Deep Note.md"]),
                (nid["Projects/Garden.md"], nid["Index.md"]),
            },
        )

    def test_shortest_path_wins_for_duplicate_names(self):
        self.write("Note.md", "top\n")
        self.write("deep/inside/Note.md", "deep\n")
        self.write("a.md", "[[Note]] [[inside/Note]]\n")
        run_ = self.sync()
        nid = ids(run_.cursor)
        self.assertEqual(
            run_.links,
            {(nid["a.md"], nid["Note.md"]), (nid["a.md"], nid["deep/inside/Note.md"])},
        )

    def test_skips_hidden_excluded_and_non_markdown(self):
        self.write("keep.md", "x\n")
        self.write(".obsidian/workspace.md", "x\n")
        self.write(".trash/old.md", "x\n")
        self.write("Templates/daily.md", "x\n")
        self.write("drawing.excalidraw.md", "x\n")
        self.write("image.png", "x\n")
        paths = {f["path"] for f in self.sync(exclude_patterns="Templates, *.excalidraw.md").upserts.values()}
        self.assertEqual(paths, {"keep.md"})

    @unittest.skipIf(sys.platform == "win32", "symbolic links need privileges on Windows")
    def test_does_not_follow_symbolic_links(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("secret\n")
        os.symlink(outside, self.vault / "linked")
        os.symlink(outside / "secret.md", self.vault / "secret.md")
        self.write("keep.md", "x\n")
        self.assertEqual({f["path"] for f in self.sync().upserts.values()}, {"keep.md"})


class IncrementalSync(VaultTest):
    def test_unchanged_vault_prints_only_the_cursor(self):
        self.write("a.md", "[[b]]\n", mtime=1_700_000_000)
        self.write("b.md", "x\n", mtime=1_700_000_000)
        first = self.sync()
        second = self.sync(first.cursor)
        self.assertEqual(second.upserts, {})
        self.assertEqual(second.links | second.unlinks, set())
        self.assertEqual(second.deletes, [])
        self.assertEqual(json.loads(second.cursor), json.loads(first.cursor))

    def test_changed_note_is_read_again_and_its_links_follow(self):
        self.write("a.md", "[[b]]\n", mtime=1_700_000_000)
        self.write("b.md", "x\n", mtime=1_700_000_000)
        self.write("c.md", "x\n", mtime=1_700_000_000)
        first = self.sync()
        nid = ids(first.cursor)
        self.write("a.md", "now [[c]] instead\n", mtime=1_700_000_100)
        second = self.sync(first.cursor)
        self.assertEqual(set(second.upserts), {nid["a.md"]})
        self.assertEqual(second.unlinks, {(nid["a.md"], nid["b.md"])})
        self.assertEqual(second.links, {(nid["a.md"], nid["c.md"])})

    def test_deleted_note_is_deleted(self):
        self.write("a.md", "[[b]]\n", mtime=1_700_000_000)
        self.write("b.md", "x\n", mtime=1_700_000_000)
        first = self.sync()
        nid = ids(first.cursor)
        (self.vault / "b.md").unlink()
        second = self.sync(first.cursor)
        self.assertEqual(second.deletes, [nid["b.md"]])
        # The daemon drops the link with the record, so no unlink for it.
        self.assertEqual(second.unlinks, set())
        self.assertNotIn(nid["b.md"], json.loads(second.cursor)["notes"])
        self.assertEqual(json.loads(second.cursor)["notes"][nid["a.md"]]["l"], [])

    def test_link_resolves_when_its_target_appears(self):
        self.write("a.md", "[[Later]]\n", mtime=1_700_000_000)
        first = self.sync()
        self.assertEqual(first.links, set())
        self.write("Later.md", "here now\n", mtime=1_700_000_100)
        second = self.sync(first.cursor)
        nid = ids(second.cursor)
        self.assertEqual(set(second.upserts), {nid["Later.md"]})
        self.assertEqual(second.links, {(nid["a.md"], nid["Later.md"])})

    def test_renamed_note_is_a_delete_and_an_insert(self):
        self.write("old.md", "x\n", mtime=1_700_000_000)
        first = self.sync()
        os.rename(self.vault / "old.md", self.vault / "new.md")
        second = self.sync(first.cursor)
        self.assertEqual(second.deletes, [ids(first.cursor)["old.md"]])
        self.assertEqual([f["path"] for f in second.upserts.values()], ["new.md"])

    def test_unknown_cursor_starts_over(self):
        self.write("a.md", "x\n")
        for cursor in ("1700000000.5", "{\"v\": 99}", "not json"):
            self.assertEqual(len(self.sync(cursor).upserts), 1, cursor)


class Errors(unittest.TestCase):
    def test_missing_vault_fails_with_a_message(self):
        result, ops = run("/no/such/vault")
        self.assertEqual(result.returncode, 2)
        self.assertIn("vault_path is not a folder", result.stderr)
        self.assertEqual(ops, [])

    def test_empty_vault_path(self):
        result, _ = run("")
        self.assertEqual(result.returncode, 2)
        self.assertIn("vault_path is empty", result.stderr)

    def test_old_input_format_is_refused(self):
        result = subprocess.run(
            [sys.executable, "-I", str(ENTRY)],
            input=json.dumps({"vault_path": "/tmp"}),
            capture_output=True,
            text=True,
        encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("contract 1", result.stderr)


if __name__ == "__main__":
    unittest.main()
