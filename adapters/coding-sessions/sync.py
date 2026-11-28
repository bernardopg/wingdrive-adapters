#!/usr/bin/env python3
"""Coding agent sessions for WingDrive (contract 1): Claude Code and Codex transcripts.

Each session becomes one record with its title, project folder, branch, dates, the tools the agent
called, the files it edited and the text of the user's prompts and of the agent's answers. Tool
output and the model's reasoning are left out, and secrets found in the text are replaced before
they are printed.

Syncs are incremental: the cursor keeps each transcript file's size and modification time, so a
sync reads only the files that changed and deletes the records of files that are gone.
"""

import json
import os
import re
import sys
from pathlib import Path

# The daemon runs `python3 -I sync.py`, which leaves this folder off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wingdrive_adapter import Adapter, run  # noqa: E402

CURSOR_VERSION = 1
MAX_BODY = 200_000
MAX_TITLE = 100
CLAUDE_DEFAULT = "~/.claude/projects"
CODEX_DEFAULT = "~/.codex/sessions"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

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


class Session:
    """What one transcript file says about its session."""

    def __init__(self, agent):
        self.agent = agent
        self.id = None
        self.title = None
        self.project = ""
        self.branch = ""
        self.started = None
        self.updated = None
        self.turns = []  # (who, text)
        self.tools = {}
        self.files = []

    def seen_at(self, timestamp):
        if isinstance(timestamp, str) and timestamp:
            if self.started is None or timestamp < self.started:
                self.started = timestamp
            if self.updated is None or timestamp > self.updated:
                self.updated = timestamp

    def used(self, tool):
        if tool:
            self.tools[tool] = self.tools.get(tool, 0) + 1

    def edited(self, path):
        if path and path not in self.files:
            self.files.append(path)

    def record(self):
        prompts = [text for who, text in self.turns if who == "user"]
        title = self.title or (prompts[0] if prompts else "") or f"{self.agent} session"
        parts = []
        for who, text in self.turns:
            parts.append(("## You\n" if who == "user" else "## Agent\n") + text.strip())
        body = redact("\n\n".join(parts))
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY]
        tools = sorted(self.tools.items(), key=lambda item: (-item[1], item[0]))
        return {
            "title": redact(one_line(title, MAX_TITLE)),
            "body": body,
            "agent": self.agent,
            "project": self.project,
            "branch": self.branch,
            "started": self.started,
            "updated": self.updated,
            "prompts": len(prompts),
            "tools": ", ".join(name for name, _ in tools),
            "files": "\n".join(self.files),
        }


def read_lines(path):
    """The JSON objects of a JSONL file; lines that are not JSON objects are skipped."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                yield item


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


def parse_claude(path):
    session = Session("Claude Code")
    session.id = Path(path).stem
    for item in read_lines(path):
        kind = item.get("type")
        if kind == "custom-title" and isinstance(item.get("customTitle"), str):
            session.title = item["customTitle"]
            continue
        if kind == "summary" and isinstance(item.get("summary"), str) and not session.title:
            session.title = item["summary"]
            continue
        if kind not in ("user", "assistant") or item.get("isSidechain") or item.get("isMeta"):
            continue
        session.seen_at(item.get("timestamp"))
        if not session.project and isinstance(item.get("cwd"), str):
            session.project = item["cwd"]
        if isinstance(item.get("gitBranch"), str) and item["gitBranch"]:
            session.branch = item["gitBranch"]
        message = item.get("message") or {}
        content = message.get("content")
        if kind == "user":
            for text in text_blocks(content, {"text"}):
                if typed_by_user(text):
                    session.turns.append(("user", text))
            continue
        for text in text_blocks(content, {"text"}):
            if text.strip():
                session.turns.append(("agent", text))
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                session.used(name)
                inputs = block.get("input") or {}
                if name in EDIT_TOOLS and isinstance(inputs, dict):
                    session.edited(inputs.get("file_path") or inputs.get("notebook_path"))
    return session


PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.MULTILINE)


def parse_codex(path):
    session = Session("Codex")
    session.id = Path(path).stem
    for item in read_lines(path):
        kind = item.get("type")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if kind == "session_meta":
            # A resumed or forked session writes a new rollout file with the same session id, so
            # the file name, unique per rollout, identifies the record.
            if isinstance(payload.get("cwd"), str):
                session.project = payload["cwd"]
            git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
            if isinstance(git.get("branch"), str):
                session.branch = git["branch"]
            session.seen_at(payload.get("timestamp"))
            continue
        if kind != "response_item":
            continue
        session.seen_at(item.get("timestamp"))
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                for text in text_blocks(payload.get("content"), {"input_text"}):
                    if typed_by_user(text):
                        session.turns.append(("user", text))
            elif role == "assistant":
                for text in text_blocks(payload.get("content"), {"output_text"}):
                    if text.strip():
                        session.turns.append(("agent", text))
        elif ptype in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            session.used(name)
            patch = payload.get("input") if ptype == "custom_tool_call" else payload.get("arguments")
            if isinstance(patch, str) and "*** Begin Patch" in patch:
                for match in PATCH_FILE.finditer(patch):
                    session.edited(match.group(1).strip())
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


SOURCES = (
    # cursor key, config key, default folder, transcript glob, parser
    ("claude", "claude_dir", CLAUDE_DEFAULT, "*/*.jsonl", parse_claude),
    ("codex", "codex_dir", CODEX_DEFAULT, "**/*.jsonl", parse_codex),
)


def sync(adapter):
    known = adapter.cursor(version=CURSOR_VERSION).get("files", {})
    files = {}
    read = 0
    for prefix, key, default, pattern, parse in SOURCES:
        root = folder(adapter, key, default)
        if root is None:
            # A folder that is gone deletes its sessions below, like removed files.
            continue
        for rel, full, stat in transcripts(root, pattern):
            file_key = f"{prefix}:{rel}"
            state = [stat.st_size, stat.st_mtime_ns]
            old = known.get(file_key)
            if old and old[:2] == state:
                files[file_key] = old
                continue
            session = parse(full)
            if not session.turns:
                # Empty or tool-only transcripts are not worth a record.
                files[file_key] = state + [None]
                continue
            external_id = f"{prefix}:{session.id}"
            adapter.upsert("session", external_id, session.record())
            files[file_key] = state + [external_id]
            read += 1

    kept = {entry[2] for entry in files.values() if len(entry) > 2 and entry[2]}
    deleted = 0
    for file_key, entry in sorted(known.items()):
        if file_key in files or len(entry) < 3 or not entry[2]:
            continue
        if entry[2] not in kept:
            adapter.delete("session", entry[2])
            deleted += 1
    adapter.info(f"{len(kept)} sessions: {read} read, {deleted} deleted")
    adapter.save_cursor({"files": files}, version=CURSOR_VERSION)


def detect(adapter):
    for key, default in (("claude_dir", CLAUDE_DEFAULT), ("codex_dir", CODEX_DEFAULT)):
        path = os.path.expanduser(default)
        # Both default folders are declared in the manifest, so the sandbox shows them.
        if os.path.isdir(path):
            adapter.suggest(key, [path])


if __name__ == "__main__":
    run(sync, detect)
