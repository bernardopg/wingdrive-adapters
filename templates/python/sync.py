#!/usr/bin/env python3
"""Text notes: one WingDrive note per .txt file at the top of a folder (contract 1).

A template for Python adapters. It shows what every adapter does:

- read its config (`adapter.path("folder")`),
- print records only for what changed since the last sync, using the cursor,
- print deletions itself, because the daemon never notices that a file is gone,
- print the cursor last,
- report problems the user can fix with `InputError`.

`wingdrive_adapter.py` next to this file is the WingDrive adapter kit. Keep it as published; the
registry checks that it matches a released kit.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

# The daemon runs `python3 -I sync.py`, which leaves this folder off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wingdrive_adapter import Adapter, run  # noqa: E402

CURSOR_VERSION = 1
MAX_BODY = 200_000


def note(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return {
        "title": path.stem,
        "body": text[:MAX_BODY],
        "words": len(text.split()),
        "modified": modified.isoformat(),
    }


def sync(adapter: Adapter) -> None:
    folder = adapter.path("folder")
    # Each file's size and modification time at the last sync, by name.
    known = adapter.cursor(version=CURSOR_VERSION).get("files", {})

    files = {}
    for path in sorted(folder.glob("*.txt")):
        if not path.is_file():
            continue
        stat = path.stat()
        files[path.name] = [stat.st_size, stat.st_mtime_ns]
        if known.get(path.name) != files[path.name]:
            adapter.upsert("note", path.name, note(path))

    for name in sorted(set(known) - set(files)):
        adapter.delete("note", name)

    adapter.info(f"{len(files)} notes in {folder}")
    adapter.save_cursor({"files": files}, version=CURSOR_VERSION)


if __name__ == "__main__":
    run(sync)
