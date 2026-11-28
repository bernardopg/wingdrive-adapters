"""Builds small OpenCode databases with the tables and JSON shapes the adapter reads.

`python tests/opencode_db.py` writes `fixtures/opencode/opencode.db`, the harness case's database;
`tests/test_sync.py` checks that the committed file still holds what this script writes.
"""

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIXTURE = HERE / "fixtures" / "opencode" / "opencode.db"

V2_SCHEMA = """
CREATE TABLE session_v2 (id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
  slug text NOT NULL, directory text NOT NULL, title text, version text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, time_archived integer);
CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL,
  seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL,
  data text NOT NULL);
"""

V1_SCHEMA = """
CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
  slug text NOT NULL, directory text NOT NULL, title text NOT NULL, version text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
"""

# 2026-11-28T10:00:00.000Z
T0 = 1795860000000

PATCH = "*** Begin Patch\n*** Update File: src/menu.ts\n@@\n-old\n+new\n*** End Patch\n"


def tool(name, inputs, call):
    return {"type": "tool", "id": call, "name": name,
            "state": {"status": "completed", "input": inputs,
                      "content": [{"type": "text", "text": "output the adapter leaves out"}]}}


def build_v2(path):
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(V2_SCHEMA)
    sessions = [
        ("ses_main", None, "/home/ana/shop", "Add a menu to the shop", T0, T0 + 60_000),
        ("ses_child", "ses_main", "/home/ana/shop", "Subagent work", T0 + 10_000, T0 + 20_000),
        ("ses_tools", None, "/home/ana/shop", "", T0 + 30_000, T0 + 40_000),
    ]
    for sid, parent, directory, title, created, updated in sessions:
        conn.execute(
            "INSERT INTO session_v2 (id, project_id, parent_id, slug, directory, title, version,"
            " time_created, time_updated) VALUES (?, 'prj', ?, ?, ?, ?, '1.3.0', ?, ?)",
            (sid, parent, sid, directory, title or None, created, updated),
        )
    messages = [
        ("ses_main", "system", {"text": "You are OpenCode."}),
        ("ses_main", "user", {"text": "Add a menu to the shop. My key is sk-proj-" + "a" * 30}),
        ("ses_main", "synthetic", {"text": "Text OpenCode adds itself"}),
        ("ses_main", "assistant", {"content": [
            {"type": "reasoning", "text": "hidden reasoning"},
            {"type": "text", "text": "I will add it."},
            tool("read", {"filePath": "/home/ana/shop/src/app.ts"}, "c1"),
            tool("edit", {"filePath": "/home/ana/shop/src/app.ts", "oldString": "a", "newString": "b"}, "c2"),
            tool("apply_patch", {"patchText": PATCH}, "c3"),
        ]}),
        ("ses_main", "assistant", {"content": [{"type": "text", "text": "The menu is in place."}]}),
        ("ses_child", "user", {"text": "Look for the menu styles"}),
        ("ses_child", "assistant", {"content": [{"type": "text", "text": "Found them."}]}),
        ("ses_tools", "assistant", {"content": [tool("bash", {"command": "ls"}, "c4")]}),
    ]
    for seq, (sid, kind, data) in enumerate(messages):
        when = T0 + 1000 * (seq + 1)
        conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"msg{seq:02d}", sid, kind, seq, when, when, json.dumps(data, sort_keys=True)),
        )
    conn.commit()
    conn.close()


def build_v1(path):
    path = Path(path)
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO session VALUES ('ses_old', 'prj', NULL, 'old', '/home/ana/blog',"
                 " 'Fix the feed', '1.0.0', ?, ?)", (T0, T0 + 5000))
    rows = [
        ("m1", {"role": "user"}, [{"type": "text", "text": "The feed is empty"},
                                  {"type": "text", "text": "added by OpenCode", "synthetic": True}]),
        ("m2", {"role": "assistant"}, [
            {"type": "reasoning", "text": "hidden"},
            {"type": "tool", "tool": "write", "state": {"input": {"filePath": "/home/ana/blog/feed.xml"}}},
            {"type": "text", "text": "Rebuilt the feed."},
        ]),
    ]
    for index, (mid, message, parts) in enumerate(rows):
        when = T0 + 1000 * (index + 1)
        conn.execute("INSERT INTO message VALUES (?, 'ses_old', ?, ?, ?)", (mid, when, when, json.dumps(message)))
        for number, part in enumerate(parts):
            conn.execute("INSERT INTO part VALUES (?, ?, 'ses_old', ?, ?, ?)",
                         (f"{mid}p{number}", mid, when + number, when + number, json.dumps(part)))
    conn.commit()
    conn.close()


def dump(path):
    conn = sqlite3.connect(str(path))
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    build_v2(FIXTURE)
    sys.stdout.write(f"wrote {FIXTURE}\n")
