#!/usr/bin/env python3
"""Obsidian adapter for WingDrive (contract 1).

Reads every Markdown note in a vault and prints one record per note, with its
frontmatter tags and aliases, and one link per resolved [[wikilink]] or
Markdown link to another note.

Syncs are incremental. The cursor remembers, for each note, its path, size and
modification time, the link targets it had and the links already printed. A
sync reads only new and changed notes, deletes the records of notes that are
gone, and adds or removes links so they match the vault, including links that
start resolving because their target note appeared.

Only the Python standard library is used.
"""

import fnmatch
import hashlib
import json
import os
import re
import posixpath
import sys
from datetime import datetime, timezone
from urllib.parse import unquote as url_unquote

CURSOR_VERSION = 1
MAX_BODY = 200_000
ALWAYS_SKIPPED = (".obsidian", ".trash", ".git", "node_modules")

FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
COMMENT = re.compile(r"%%.*?%%", re.DOTALL)
WIKILINK = re.compile(r"!?\[\[([^\]\|#\^]*)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")
MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(<?([^)>\s]+\.md)(?:#[^)>\s]*)?>?(?:\s+\"[^\"]*\")?\)")
INLINE_TAG = re.compile(r"(?:^|(?<=\s))#([\w/-]*[^\W\d_][\w/-]*)", re.UNICODE)


class InputError(Exception):
    """A problem with the input the user can fix: printed to stderr, exit 2."""


def emit(operation):
    sys.stdout.write(json.dumps(operation, ensure_ascii=False) + "\n")


def log(level, message):
    emit({"log": level, "message": message})


# ── Frontmatter ─────────────────────────────────────────────────────────────


def split_frontmatter(text):
    """Return (frontmatter text or None, body)."""
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return None, text
    lines = text.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") in ("---", "..."):
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, text


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def strip_comment(value):
    """Drop a trailing ` # comment` outside quotes."""
    quote = None
    for index, char in enumerate(value):
        if char in "'\"":
            quote = None if quote == char else (quote or char)
        elif char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def parse_frontmatter(source):
    """Parse the YAML subset notes use: scalars, [inline, lists] and block lists.

    Nested mappings are skipped. Values stay strings; lists are lists of strings.
    """
    data = {}
    key = None
    for raw in source.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if raw[0] in " \t" or stripped.startswith("- "):
            if key is not None and stripped.startswith("-"):
                item = unquote(strip_comment(stripped[1:].strip()))
                if item:
                    if not isinstance(data.get(key), list):
                        data[key] = []
                    data[key].append(item)
            continue
        name, sep, value = raw.partition(":")
        if not sep:
            key = None
            continue
        key = name.strip().lower()
        value = strip_comment(value.strip())
        if value.startswith("[") and value.endswith("]"):
            data[key] = [unquote(v) for v in value[1:-1].split(",") if unquote(v)]
        elif value in ("", "|", ">"):
            data[key] = []
        else:
            data[key] = unquote(value)
    return data


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in re.split(r"[,\s]+", str(value)) if part.strip()]


def normalize_date(value):
    if not value or isinstance(value, list):
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00"), text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


# ── Notes ───────────────────────────────────────────────────────────────────


def note_id(rel_path):
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]


def visible_text(body):
    """The body without code and comments, where links and tags do not count."""
    return INLINE_CODE.sub(" ", FENCE.sub(" ", COMMENT.sub(" ", body)))


def link_key(target):
    """Normalize a link target the way it is looked up: lower case, `/`, no `.md`."""
    target = target.strip().replace("\\", "/").lstrip("/")
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target.lower()


def link_targets(text, folder=""):
    """Link keys a note points at. Markdown links count both relative to the note and to the vault."""
    targets = set()
    for match in WIKILINK.finditer(text):
        target = match.group(1).strip()
        if not target:
            continue
        extension = os.path.splitext(target)[1].lower()
        if extension and extension != ".md" and not extension[1:].isdigit():
            continue  # an embed of an image or PDF, not a note
        targets.add(link_key(target))
    for match in MD_LINK.finditer(text):
        target = match.group(1)
        if "://" in target:
            continue
        target = url_unquote(target)
        targets.add(link_key(target))
        relative = posixpath.normpath(posixpath.join(folder, target))
        if not relative.startswith("../"):
            targets.add(link_key(relative))
    return sorted(targets)


def read_note(full_path, rel_path, stat):
    with open(full_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    raw_frontmatter, body = split_frontmatter(text)
    frontmatter = parse_frontmatter(raw_frontmatter) if raw_frontmatter else {}
    visible = visible_text(body)

    tags = {t.lstrip("#") for t in as_list(frontmatter.get("tags", frontmatter.get("tag")))}
    tags.update(INLINE_TAG.findall(visible))
    tags.discard("")
    aliases = as_list(frontmatter.get("aliases", frontmatter.get("alias")))
    if isinstance(frontmatter.get("aliases"), str):
        aliases = [frontmatter["aliases"]]

    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    birth = getattr(stat, "st_birthtime", None)
    created = (
        normalize_date(frontmatter.get("created"))
        or normalize_date(frontmatter.get("date"))
        or (datetime.fromtimestamp(birth, tz=timezone.utc).isoformat() if birth else modified)
    )
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else None
    folder = os.path.dirname(rel_path)

    body = drop_title_heading(body.strip(), title or stem)
    fields = {
        "title": title or stem,
        "body": body if len(body) <= MAX_BODY else body[:MAX_BODY],
        "path": rel_path,
        "folder": folder,
        "tags": ", ".join(sorted(tags, key=str.lower)),
        "aliases": ", ".join(aliases),
        "created": created,
        "modified": modified,
        "word_count": len(re.findall(r"\w+", visible)),
    }
    return fields, link_targets(visible, folder), aliases


def drop_title_heading(body, title):
    """Drops a first `# Heading` that only repeats the note's title, so previews start with text."""
    first, _, rest = body.partition("\n")
    if first.startswith("# ") and first[2:].strip().lower() == title.strip().lower():
        return rest.lstrip("\n")
    return body


# ── Vault ───────────────────────────────────────────────────────────────────


def excluded(rel_path, patterns):
    parts = rel_path.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern) or any(fnmatch.fnmatch(p, pattern) for p in parts):
            return True
    return False


def scan(vault, patterns):
    """Yield (rel_path, full_path, stat) for every note, skipping links and hidden folders."""
    for root, dirs, files in os.walk(vault):
        rel_root = os.path.relpath(root, vault).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root + "/"
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and d not in ALWAYS_SKIPPED
            and not os.path.islink(os.path.join(root, d))
            and not excluded(rel_root + d, patterns)
        )
        for name in sorted(files):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = rel_root + name
            if os.path.islink(full) or excluded(rel, patterns):
                continue
            try:
                yield rel, full, os.stat(full)
            except OSError:
                continue


class Resolver:
    """Finds the note a link points at, as Obsidian does: by path, then name, then alias."""

    def __init__(self, notes):
        self.by_path = {}
        self.by_name = {}
        self.by_alias = {}
        for nid, state in notes.items():
            key = link_key(state["p"])
            self.by_path[key] = nid
            self.by_name.setdefault(key.rsplit("/", 1)[-1], []).append((key, nid))
            for alias in state.get("a", []):
                self.by_alias.setdefault(alias.lower(), nid)
        for candidates in self.by_name.values():
            candidates.sort(key=lambda c: (c[0].count("/"), c[0]))

    def resolve(self, target):
        if target in self.by_path:
            return self.by_path[target]
        name = target.rsplit("/", 1)[-1]
        candidates = self.by_name.get(name, [])
        if "/" in target:
            candidates = [c for c in candidates if c[0].endswith("/" + target)]
        if candidates:
            return candidates[0][1]
        return self.by_alias.get(target)


def load_cursor(raw):
    if not raw:
        return {}
    try:
        cursor = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(cursor, dict) or cursor.get("v") != CURSOR_VERSION:
        return {}
    notes = cursor.get("notes")
    return notes if isinstance(notes, dict) else {}


def sync(payload):
    config = payload.get("config") or {}
    vault = os.path.expanduser(str(config.get("vault_path") or "").strip())
    if not vault:
        raise InputError("vault_path is empty: choose the folder of your Obsidian vault")
    if not os.path.isdir(vault):
        raise InputError(f"vault_path is not a folder: {vault}")
    patterns = [p.strip().strip("/") for p in str(config.get("exclude_patterns") or "").split(",")]
    patterns = [p for p in patterns if p]

    previous = load_cursor(payload.get("cursor"))
    current = {}
    changed = []
    for rel, full, stat in scan(vault, patterns):
        nid = note_id(rel)
        old = previous.get(nid)
        if old and old.get("m") == stat.st_mtime_ns and old.get("s") == stat.st_size:
            current[nid] = dict(old)
        else:
            current[nid] = {"p": rel, "m": stat.st_mtime_ns, "s": stat.st_size, "t": [], "a": [], "l": []}
            changed.append((nid, rel, full, stat))

    deleted = [nid for nid in previous if nid not in current]
    for nid in deleted:
        emit({"delete": "note", "external_id": nid})

    unreadable = 0
    for nid, rel, full, stat in changed:
        try:
            fields, targets, aliases = read_note(full, rel, stat)
        except OSError as error:
            unreadable += 1
            log("warn", f"could not read {rel}: {error.strerror or error}")
            current.pop(nid)
            if nid in previous:
                # Keep the old record and links; retry next sync.
                current[nid] = dict(previous[nid], m=-1)
            continue
        current[nid]["t"] = targets
        current[nid]["a"] = aliases
        current[nid]["l"] = list(previous.get(nid, {}).get("l", []))
        emit({"upsert": "note", "external_id": nid, "fields": fields})

    resolver = Resolver(current)
    added = removed = 0
    for nid in sorted(current):
        state = current[nid]
        wanted = {resolver.resolve(t) for t in state.get("t", [])}
        wanted.discard(None)
        wanted.discard(nid)
        had = set(state.get("l", []))
        for target in sorted(had - wanted):
            if target in current:
                emit({"unlink": "note", "id": nid, "to": "note", "to_id": target})
                removed += 1
        for target in sorted(wanted - had):
            emit({"link": "note", "id": nid, "to": "note", "to_id": target})
            added += 1
        state["l"] = sorted(wanted)

    emit({"cursor": json.dumps({"v": CURSOR_VERSION, "notes": current}, separators=(",", ":"))})
    log(
        "info",
        f"{len(current)} notes: {len(changed) - unreadable} read, {len(deleted)} deleted, "
        f"{added} links added, {removed} removed",
    )


# ── Detect ──────────────────────────────────────────────────────────────────


def obsidian_configs():
    """Where Obsidian keeps its list of vaults on each system and install type."""
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, ".config", "obsidian", "obsidian.json"),
        os.path.join(home, ".var", "app", "md.obsidian.Obsidian", "config", "obsidian", "obsidian.json"),
        os.path.join(home, "snap", "obsidian", "current", ".config", "obsidian", "obsidian.json"),
        os.path.join(home, "Library", "Application Support", "obsidian", "obsidian.json"),
    ]
    if os.environ.get("APPDATA"):
        paths.append(os.path.join(os.environ["APPDATA"], "obsidian", "obsidian.json"))
    return paths


def detect():
    """Suggest the vaults Obsidian knows that still have their .obsidian folder, most recent first."""
    vaults = []
    for path in obsidian_configs():
        try:
            with open(path, encoding="utf-8") as handle:
                known = json.load(handle).get("vaults") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for vault in known.values():
            if isinstance(vault, dict) and isinstance(vault.get("path"), str):
                vaults.append((vault.get("ts") or 0, vault["path"]))
    # Inside WingDrive's sandbox the vault folders are not mounted during detection, so their
    # existence can only be checked outside it.
    confined = os.environ.get("WINGDRIVE_ADAPTER_ISOLATION") == "bubblewrap"
    paths = []
    for _, path in sorted(vaults, key=lambda v: v[0] if isinstance(v[0], (int, float)) else 0, reverse=True):
        # A vault is a folder with an .obsidian settings folder; one Obsidian still lists after the
        # folder was moved or deleted is not offered.
        if path not in paths and (confined or os.path.isdir(os.path.join(path, ".obsidian"))):
            paths.append(path)
    if paths:
        emit({"suggest": {"vault_path": paths}})


def main():
    # The daemon speaks UTF-8 on every platform; Windows consoles default to a code page.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("contract") != 1:
            raise InputError("expected a contract 1 input document on stdin")
        if payload.get("mode") == "detect":
            detect()
            return 0
        sync(payload)
    except InputError as error:
        print(error, file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"stdin is not JSON: {error}", file=sys.stderr)
        return 2
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
