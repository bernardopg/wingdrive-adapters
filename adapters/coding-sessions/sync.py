#!/usr/bin/env python3
"""Coding agent sessions for WingDrive (contract 1): Claude Code, Codex and OpenCode transcripts.

Each session becomes one record with its title, a short summary, project folder, branch, dates, the
tools the agent called, the files it edited and the full text of the user's prompts and of the
agent's answers. Tools and edited files are also records of their own, linked to the sessions that
used them. Tool output and the model's reasoning are left out, and secrets found in the text are
replaced before they are printed.

Syncs are incremental. Claude Code and Codex append to their JSONL transcripts, so the adapter keeps
how far it read each file and what it found there in its state folder, and reads only the new lines;
a file that shrank or whose bytes before that point changed is read again from the start. OpenCode
keeps its sessions in a SQLite database, opened read only, and a session is read again when its
message count or last update changes. Sessions whose file or row is gone are deleted.
"""

import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# The daemon runs `python3 -I sync.py`, which leaves this folder off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wingdrive_adapter import run  # noqa: E402

CURSOR_VERSION = 2
STATE_VERSION = 1
MAX_TITLE = 100
SUMMARY_PART = 400
CHECK_BYTES = 4096
CLAUDE_DEFAULT = "~/.claude/projects"
CODEX_DEFAULT = "~/.codex/sessions"
# OpenCode keeps its data under the XDG data folder on every platform, macOS and Windows included.
OPENCODE_DEFAULT = "~/.local/share/opencode/opencode.db"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
OPENCODE_EDIT_TOOLS = {"edit", "write", "multiedit"}

# Secrets that appear in prompts and answers. Each match becomes "[secret]".
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"
    ),
]

# Text the agents add to user turns themselves, not something the user typed.
INJECTED_PREFIXES = (
    "<command-",
    "<local-command",
    "<system-reminder>",
    "<environment_context>",
    "<user_instructions>",
    "<permissions instructions>",
    "Caveat:",
    "# AGENTS.md instructions",
)

PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.MULTILINE)


def redact(text):
    """`text` with every secret it holds replaced by "[secret]"."""
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[secret]{m.group(3)}", text)
        else:
            text = pattern.sub("[secret]", text)
    return text


def typed_by_user(text):
    stripped = text.lstrip()
    return bool(stripped) and not stripped.startswith(INJECTED_PREFIXES)


def one_line(text, limit):
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def absolute(path, project):
    """`path` joined to the session's folder when the agent recorded it relative to it."""
    if not path or not project or path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", path):
        return path
    windows = "\\" in project and "/" not in project
    sep = "\\" if windows else "/"
    return project.rstrip("/\\") + sep + (path.replace("/", "\\") if windows else path)


def iso_millis(value):
    """An OpenCode timestamp (milliseconds since the epoch) in the ISO 8601 form the others use."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(value) % 1000:03d}Z"


class Session:
    """What one transcript says about its session. Saved in the state folder between syncs."""

    FIELDS = ("agent", "id", "title", "summary_title", "project", "branch", "started", "updated",
              "turns", "tools", "files")

    def __init__(self, agent):
        self.agent = agent
        self.id = None
        self.title = None
        # Claude Code writes a generated summary line; a title the user set wins over it.
        self.summary_title = None
        self.project = ""
        self.branch = ""
        self.started = None
        self.updated = None
        self.turns = []  # [who, text]
        self.tools = {}
        self.files = []

    def state(self):
        return {name: getattr(self, name) for name in self.FIELDS}

    @classmethod
    def from_state(cls, state):
        session = cls(state["agent"])
        for name in cls.FIELDS:
            if name in state:
                setattr(session, name, state[name])
        return session

    def seen_at(self, timestamp):
        if isinstance(timestamp, str) and timestamp:
            if self.started is None or timestamp < self.started:
                self.started = timestamp
            if self.updated is None or timestamp > self.updated:
                self.updated = timestamp

    def said(self, who, text):
        self.turns.append([who, text])

    def used(self, tool):
        if isinstance(tool, str) and tool:
            self.tools[tool] = self.tools.get(tool, 0) + 1

    def edited(self, path):
        if isinstance(path, str) and path.strip() and path not in self.files:
            self.files.append(path.strip())

    def edited_files(self):
        found = []
        for path in self.files:
            full = absolute(path, self.project)
            if full not in found:
                found.append(full)
        return found

    def tool_names(self):
        ranked = sorted(self.tools.items(), key=lambda item: (-item[1], item[0]))
        return [name for name, _ in ranked]

    def summary(self):
        """The first prompt and the last answer, short enough for a preview."""
        prompts = [text for who, text in self.turns if who == "user"]
        answers = [text for who, text in self.turns if who != "user"]
        parts = []
        if prompts:
            parts.append(one_line(prompts[0], SUMMARY_PART))
        if answers:
            parts.append(one_line(answers[-1], SUMMARY_PART))
        return redact("\n\n".join(parts))

    def record(self):
        prompts = [text for who, text in self.turns if who == "user"]
        title = self.title or self.summary_title or (prompts[0] if prompts else "") or f"{self.agent} session"
        parts = []
        for who, text in self.turns:
            parts.append(("## You\n" if who == "user" else "## Agent\n") + text.strip())
        return {
            "title": redact(one_line(title, MAX_TITLE)),
            "summary": self.summary(),
            "body": redact("\n\n".join(parts)),
            "agent": self.agent,
            "project": self.project,
            "branch": self.branch,
            "started": self.started,
            "updated": self.updated,
            "prompts": len(prompts),
            "tools": ", ".join(self.tool_names()),
            "files": "\n".join(self.edited_files()),
        }


def text_blocks(content, kinds):
    """The text of a message's content, a string or a list of typed blocks."""
    if isinstance(content, str):
        return [content]
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in kinds and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return texts


def read_claude(session, item):
    kind = item.get("type")
    if kind == "custom-title" and isinstance(item.get("customTitle"), str):
        session.title = item["customTitle"]
        return
    if kind == "summary" and isinstance(item.get("summary"), str):
        session.summary_title = item["summary"]
        return
    if kind not in ("user", "assistant") or item.get("isSidechain") or item.get("isMeta"):
        return
    session.seen_at(item.get("timestamp"))
    if not session.project and isinstance(item.get("cwd"), str):
        session.project = item["cwd"]
    if isinstance(item.get("gitBranch"), str) and item["gitBranch"]:
        session.branch = item["gitBranch"]
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    content = message.get("content")
    if kind == "user":
        for text in text_blocks(content, {"text"}):
            if typed_by_user(text):
                session.said("user", text)
        return
    for text in text_blocks(content, {"text"}):
        if text.strip():
            session.said("agent", text)
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            session.used(name)
            inputs = block.get("input") or {}
            if name in EDIT_TOOLS and isinstance(inputs, dict):
                session.edited(inputs.get("file_path") or inputs.get("notebook_path"))


def read_codex(session, item):
    kind = item.get("type")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if kind == "session_meta":
        if isinstance(payload.get("cwd"), str):
            session.project = payload["cwd"]
        git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
        if isinstance(git.get("branch"), str):
            session.branch = git["branch"]
        session.seen_at(payload.get("timestamp"))
        return
    if kind != "response_item":
        return
    session.seen_at(item.get("timestamp"))
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role == "user":
            for text in text_blocks(payload.get("content"), {"input_text"}):
                if typed_by_user(text):
                    session.said("user", text)
        elif role == "assistant":
            for text in text_blocks(payload.get("content"), {"output_text"}):
                if text.strip():
                    session.said("agent", text)
    elif ptype in ("function_call", "custom_tool_call"):
        session.used(payload.get("name"))
        patch = payload.get("input") if ptype == "custom_tool_call" else payload.get("arguments")
        patched(session, patch)


def patched(session, patch):
    if isinstance(patch, str) and "*** Begin Patch" in patch:
        for match in PATCH_FILE.finditer(patch):
            session.edited(match.group(1).strip())


def read_from(path, offset, handle_item):
    """Pass each JSON object of a JSONL file after byte `offset` to `handle_item`; return the
    offset to resume from.

    A line without its newline yet is left for the next sync, unless it already holds a whole JSON
    object: an agent that stopped writing mid-line resumes at the same offset either way.
    """
    with open(path, "rb") as handle:
        handle.seek(offset)
        for line in handle:
            item = parse_line(line)
            if line.endswith(b"\n") or item is not None:
                offset += len(line)
            if item is not None:
                handle_item(item)
    return offset


def parse_line(line):
    line = line.strip()
    if not line:
        return None
    try:
        item = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def digest(path, start, length):
    with open(path, "rb") as handle:
        handle.seek(start)
        return hashlib.sha256(handle.read(length)).hexdigest()


def marks(path, offset):
    """Hashes of the first bytes and of the bytes just before `offset`, to notice a rewrite."""
    head = min(offset, CHECK_BYTES)
    tail_start = max(0, offset - CHECK_BYTES)
    return [digest(path, 0, head), digest(path, tail_start, offset - tail_start)]


class State:
    """Per-transcript parse state and the links of every session, kept in the source's state folder.

    Without a state folder (detect mode, or an adapter run by hand) every changed transcript is
    read whole and links are only added.
    """

    def __init__(self, data_dir):
        self.root = Path(data_dir) if data_dir else None
        self.links = {}
        if self.root is not None:
            loaded = self._load(self.root / "links.json.gz")
            if isinstance(loaded, dict) and loaded.get("v") == STATE_VERSION:
                self.links = loaded.get("sessions", {})

    def _load(self, path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError, EOFError):
            return None

    def _save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        with gzip.open(partial, "wt", encoding="utf-8", compresslevel=5) as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(partial, path)

    def _file(self, file_key):
        name = hashlib.sha256(file_key.encode("utf-8")).hexdigest()[:32]
        return self.root / "transcripts" / f"{name}.json.gz"

    def transcript(self, file_key):
        if self.root is None:
            return None
        loaded = self._load(self._file(file_key))
        if isinstance(loaded, dict) and loaded.get("v") == STATE_VERSION and loaded.get("key") == file_key:
            return loaded
        return None

    def save_transcript(self, file_key, offset, check, session):
        if self.root is not None:
            self._save(self._file(file_key), {"v": STATE_VERSION, "key": file_key, "offset": offset,
                                              "marks": check, "session": session.state()})

    def forget_transcript(self, file_key):
        if self.root is not None:
            try:
                self._file(file_key).unlink()
            except OSError:
                pass

    def save_links(self):
        if self.root is not None:
            self._save(self.root / "links.json.gz", {"v": STATE_VERSION, "sessions": self.links})


def read_transcript(state, file_key, full, size, reader, agent):
    """The session in one JSONL transcript, reading only what was appended since the last sync."""
    saved = state.transcript(file_key)
    session, offset = None, 0
    if saved is not None:
        old = saved.get("offset", 0)
        try:
            same = isinstance(old, int) and 0 < old <= size and marks(full, old) == saved.get("marks")
        except OSError:
            same = False
        if same:
            session, offset = Session.from_state(saved["session"]), old
    if session is None:
        session = Session(agent)
        session.id = Path(full).stem
    offset = read_from(full, offset, lambda item: reader(session, item))
    state.save_transcript(file_key, offset, marks(full, offset), session)
    return session


def transcripts(root, pattern):
    """(relative path, full path, stat) of each transcript below `root`, in a stable order."""
    found = []
    for path in sorted(Path(root).glob(pattern)):
        try:
            if path.is_file() and not path.is_symlink():
                found.append((path.relative_to(root).as_posix(), path, path.stat()))
        except OSError:
            continue
    return found


def folder(adapter, key, default):
    """The folder for one agent, or None when it does not exist (that agent is not installed)."""
    value = adapter.get(key)
    path = Path(os.path.expanduser(value or default))
    if path.is_dir():
        return path
    if value:
        # The user chose this folder, so a missing one is worth saying.
        adapter.warn(f"{key}: {path} is not a folder; skipping it")
    return None


JSONL_SOURCES = (
    # cursor prefix, config key, default folder, transcript glob, line reader, agent
    ("claude", "claude_dir", CLAUDE_DEFAULT, "*/*.jsonl", read_claude, "Claude Code"),
    ("codex", "codex_dir", CODEX_DEFAULT, "**/*.jsonl", read_codex, "Codex"),
)


# ── OpenCode ────────────────────────────────────────────────────────────────


def open_readonly(path):
    """A read-only connection to an SQLite database another program keeps open.

    OpenCode uses write-ahead logging. SQLite reads such a database read only as long as its `-wal`
    and `-shm` files exist, which they do while OpenCode runs. When they are gone every change is
    already in the main file, so it is safe to open it as immutable, which needs neither.
    """
    uri = Path(path).resolve().as_uri()
    try:
        return checked(sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10))
    except sqlite3.Error:
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise
    return checked(sqlite3.connect(uri + "?immutable=1", uri=True))


def checked(conn):
    """`conn` once a first read works; closed otherwise, so Windows does not keep the file locked."""
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def loads(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def tables(conn):
    return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


class OpenCode:
    """OpenCode's database: `session_v2` and `session_message` since 1.3, `session`, `message` and
    `part` before. Child sessions (subagents the main session started) are left out, like Claude
    Code's sidechains."""

    def __init__(self, conn):
        self.conn = conn
        names = tables(conn)
        if {"session_v2", "session_message"} <= names:
            self.version = 2
        elif {"session", "message", "part"} <= names:
            self.version = 1
        else:
            raise sqlite3.DatabaseError("no OpenCode session tables")

    def sessions(self):
        """{session id: (stamp, title, folder, created)} for every top-level session."""
        if self.version == 2:
            rows = self.conn.execute(
                "SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,"
                " count(m.id), max(m.time_updated)"
                " FROM session_v2 s LEFT JOIN session_message m ON m.session_id = s.id"
                " WHERE s.parent_id IS NULL GROUP BY s.id ORDER BY s.id"
            )
        else:
            rows = self.conn.execute(
                "SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,"
                " (SELECT count(*) FROM part p WHERE p.session_id = s.id),"
                " (SELECT max(p.time_updated) FROM part p WHERE p.session_id = s.id)"
                " FROM session s WHERE s.parent_id IS NULL ORDER BY s.id"
            )
        found = {}
        for sid, title, directory, created, updated, count, last in rows:
            found[str(sid)] = (f"{updated}:{count}:{last}", title, directory, created)
        return found

    def read(self, sid, title, directory, created):
        session = Session("OpenCode")
        session.id = sid
        session.title = title if isinstance(title, str) and title.strip() else None
        session.project = directory if isinstance(directory, str) else ""
        session.seen_at(iso_millis(created))
        if self.version == 2:
            rows = self.conn.execute(
                "SELECT type, data, time_created FROM session_message WHERE session_id = ? ORDER BY seq",
                (sid,),
            )
            for kind, data, when in rows:
                self._message_v2(session, kind, loads(data), when)
        else:
            rows = self.conn.execute(
                "SELECT m.data, p.data, p.time_created FROM message m JOIN part p ON p.message_id = m.id"
                " WHERE m.session_id = ? ORDER BY m.time_created, m.id, p.id",
                (sid,),
            )
            for message, part, when in rows:
                self._part_v1(session, loads(message), loads(part), when)
        return session

    def _message_v2(self, session, kind, data, when):
        if kind == "user":
            # "synthetic" and "system" messages are text OpenCode adds itself.
            text = data.get("text")
            if isinstance(text, str) and typed_by_user(text):
                session.seen_at(iso_millis(when))
                session.said("user", text)
            return
        if kind != "assistant":
            return
        session.seen_at(iso_millis(when))
        for block in data.get("content") if isinstance(data.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                session.said("agent", block["text"])
            elif block.get("type") == "tool":
                state = block.get("state") if isinstance(block.get("state"), dict) else {}
                self._tool(session, block.get("name"), state.get("input"))

    def _part_v1(self, session, message, part, when):
        role = message.get("role")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and not part.get("synthetic"):
            text = part["text"]
            if role == "user" and typed_by_user(text):
                session.seen_at(iso_millis(when))
                session.said("user", text)
            elif role == "assistant" and text.strip():
                session.seen_at(iso_millis(when))
                session.said("agent", text)
        elif kind == "tool" and role == "assistant":
            session.seen_at(iso_millis(when))
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            self._tool(session, part.get("tool"), state.get("input"))

    def _tool(self, session, name, inputs):
        session.used(name)
        inputs = inputs if isinstance(inputs, dict) else {}
        if name in OPENCODE_EDIT_TOOLS:
            session.edited(inputs.get("filePath") or inputs.get("path"))
        elif name == "apply_patch":
            patched(session, inputs.get("patchText"))


def opencode_database(adapter):
    """The OpenCode database to read, or None when OpenCode is not installed."""
    value = adapter.get("opencode_db")
    path = Path(os.path.expanduser(value or OPENCODE_DEFAULT))
    if path.is_file():
        return path
    if value:
        adapter.warn(f"opencode_db: {path} is not a file; skipping it")
    return None


# ── Sync ────────────────────────────────────────────────────────────────────


class Links:
    """Keeps each session's links to its tools and files and the tool and file records they need."""

    MODELS = (("tool", "tool_names"), ("file", "edited_files"))

    def __init__(self, adapter, state):
        self.adapter = adapter
        self.state = state
        self.before = self._targets()

    def _targets(self):
        targets = {model: set() for model, _ in self.MODELS}
        for links in self.state.links.values():
            for model, _ in self.MODELS:
                targets[model].update(links.get(model, []))
        return targets

    def update(self, external_id, session):
        old = self.state.links.get(external_id, {})
        new = {}
        for model, getter in self.MODELS:
            wanted = getattr(session, getter)()
            new[model] = wanted
            for target in wanted:
                if target not in self.before[model]:
                    self.adapter.upsert(model, target, self._fields(model, target))
                    self.before[model].add(target)
            had = set(old.get(model, []))
            for target in wanted:
                if target not in had:
                    self.adapter.link("session", external_id, model, target)
            for target in sorted(had - set(wanted)):
                self.adapter.unlink("session", external_id, model, target)
        self.state.links[external_id] = new

    def removed(self, external_id):
        self.state.links.pop(external_id, None)

    def finish(self):
        """Delete the tool and file records no session links to any more."""
        now = self._targets()
        for model, _ in self.MODELS:
            for target in sorted(self.before[model] - now[model]):
                self.adapter.delete(model, target)
        self.state.save_links()

    @staticmethod
    def _fields(model, target):
        if model == "tool":
            return {"name": target}
        name = re.split(r"[\\/]", target.rstrip("/\\"))[-1]
        return {"path": target, "name": name}


def sync(adapter):
    cursor = adapter.cursor(version=CURSOR_VERSION)
    # A cursor from 0.1.0 still names the sessions to delete, but every session is read again so
    # its record gains the summary and its links.
    migrating = not cursor and bool(adapter.cursor(version=1))
    known = cursor.get("files") or adapter.cursor(version=1).get("files", {})
    state = State(adapter.data_dir)
    links = Links(adapter, state)
    seen = {}
    read = 0

    def upsert(entry_key, stamp, session):
        nonlocal read
        if not session.turns:
            # Empty or tool-only sessions are not worth a record.
            seen[entry_key] = stamp + [None]
            return
        external_id = f"{entry_key.split(':', 1)[0]}:{session.id}"
        adapter.upsert("session", external_id, session.record())
        links.update(external_id, session)
        seen[entry_key] = stamp + [external_id]
        read += 1

    for prefix, key, default, pattern, reader, agent in JSONL_SOURCES:
        root = folder(adapter, key, default)
        if root is None:
            # A folder that is gone deletes its sessions below, like removed files.
            continue
        for rel, full, stat in transcripts(root, pattern):
            entry_key = f"{prefix}:{rel}"
            stamp = [stat.st_size, stat.st_mtime_ns]
            old = known.get(entry_key)
            if old and old[:2] == stamp and not migrating:
                seen[entry_key] = old
                continue
            try:
                session = read_transcript(state, entry_key, full, stat.st_size, reader, agent)
            except OSError as error:
                # Unreadable for now (permissions, a file being replaced): try again next sync.
                adapter.warn(f"{key}: cannot read {rel}: {error}")
                if old:
                    seen[entry_key] = old
                continue
            upsert(entry_key, stamp, session)

    database = opencode_database(adapter)
    if database is not None:
        try:
            conn = open_readonly(database)
            try:
                opencode = OpenCode(conn)
                for sid, (stamp, title, directory, created) in opencode.sessions().items():
                    entry_key = f"opencode:{sid}"
                    old = known.get(entry_key)
                    if old and old[:1] == [stamp] and not migrating:
                        seen[entry_key] = old
                        continue
                    upsert(entry_key, [stamp], opencode.read(sid, title, directory, created))
            finally:
                conn.close()
        except sqlite3.Error as error:
            # Keep OpenCode's sessions as they were; deleting them over a locked or newer database
            # would lose them until the next good sync.
            adapter.warn(f"opencode_db: cannot read {database}: {error}")
            for entry_key, entry in known.items():
                if entry_key.startswith("opencode:"):
                    seen[entry_key] = entry

    kept = {entry[-1] for entry in seen.values() if entry and entry[-1]}
    gone = set()
    for entry_key, entry in sorted(known.items()):
        if entry_key not in seen:
            state.forget_transcript(entry_key)
        # A transcript that is gone, or that no longer holds a prompt, takes its session along.
        external_id = entry[-1] if entry else None
        if external_id and external_id not in kept and external_id not in gone:
            adapter.delete("session", external_id)
            links.removed(external_id)
            gone.add(external_id)
    deleted = len(gone)
    links.finish()
    adapter.info(f"{len(kept)} sessions: {read} read, {deleted} deleted")
    adapter.save_cursor({"files": seen}, version=CURSOR_VERSION)


def detect(adapter):
    defaults = (("claude_dir", CLAUDE_DEFAULT, os.path.isdir), ("codex_dir", CODEX_DEFAULT, os.path.isdir),
                ("opencode_db", OPENCODE_DEFAULT, os.path.isfile))
    for key, default, exists in defaults:
        # Path() gives the platform's separators: expanduser keeps the "/" of the default on Windows.
        path = str(Path(os.path.expanduser(default)))
        # The default locations are declared in the manifest, so the sandbox shows them.
        if exists(path):
            adapter.suggest(key, [path])


if __name__ == "__main__":
    run(sync, detect)
