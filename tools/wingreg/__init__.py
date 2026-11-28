"""Registry tooling for the WingDrive adapter store.

`wingreg validate` checks registry entries and the adapters they point at.
`wingreg build` packs every adapter into a reproducible archive and writes the index the app reads.
The publish workflow signs that index with minisign; nothing here touches keys.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_URL = "https://github.com/bernardopg/wingdrive-adapters"
ICON_MAX_BYTES = 64 * 1024
EXCLUDED = {"__pycache__", ".git", ".github", ".pytest_cache", "target", "node_modules"}
REQUIRED_README_SECTIONS = ("## Setup", "## Data", "## Privacy")
# Translated READMEs (README.<lang>.md) keep the same three sections, in their language.
# The Python adapter kit. Adapters vendor `wingdrive_adapter.py`; each copy must be a released kit,
# so a reviewer reads its code once, not once per adapter.
KIT_FILE = "wingdrive_adapter.py"
KIT_RELEASES = ROOT / "kit" / "python" / "releases.json"
TRANSLATED_README_SECTIONS = {
    "pt": ("## Configuração", "## Dados", "## Privacidade"),
    "es": ("## Configuración", "## Datos", "## Privacidad"),
}


class RegistryError(Exception):
    """A problem a submitter must fix. The message names the file and the field."""


@dataclass
class Adapter:
    """One registry entry with the manifest read from its pinned source."""

    entry: dict
    manifest: dict
    directory: Path
    problems: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.entry["id"]

    @property
    def meta(self) -> dict:
        return self.manifest["adapter"]


def _schema(name: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "schema" / name).read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_errors(validator: Draft202012Validator, data: object, where: str) -> list[str]:
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{where}: {path}: {error.message}")
    return errors


def load_entries(root: Path = ROOT) -> list[tuple[Path, dict]]:
    """Every registry/<id>.json, sorted by id."""
    entries = []
    for path in sorted((root / "registry").glob("*.json")):
        try:
            entries.append((path, json.loads(path.read_text())))
        except json.JSONDecodeError as error:
            raise RegistryError(f"{path.name}: not valid JSON: {error}") from error
    return entries


def validate_entry(path: Path, entry: dict) -> list[str]:
    problems = _schema_errors(_schema("entry.schema.json"), entry, path.name)
    if not problems and path.stem != entry["id"]:
        problems.append(f"{path.name}: file name must be {entry['id']}.json")
    if not problems and entry["source"]["type"] == "local" and entry["trust"] != "official":
        problems.append(f"{path.name}: adapters kept in this repository are official")
    if not problems and entry["source"]["type"] == "git" and entry["trust"] == "official":
        problems.append(f"{path.name}: official adapters live in adapters/<id>/ of this repository")
    return problems


def _run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RegistryError(f"`{' '.join(args)}` failed: {result.stderr.strip()}")
    return result.stdout


def fetch_source(entry: dict, root: Path, workdir: Path) -> Path:
    """Directory with the adapter at its pinned source.

    Local adapters come from this checkout. A third-party adapter is fetched at its pinned commit,
    and the tag must still point at that commit, so a moved tag fails review instead of slipping in.
    """
    source = entry["source"]
    if source["type"] == "local":
        directory = root / source["path"]
        if not directory.is_dir():
            raise RegistryError(f"{entry['id']}: {source['path']} does not exist")
        return directory

    repository, tag, commit = source["repository"], source["tag"], source["commit"]
    refs = _run("git", "ls-remote", "--tags", repository, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    tag_commits = {line.split()[0] for line in refs.splitlines() if line}
    if commit not in tag_commits:
        raise RegistryError(f"{entry['id']}: tag {tag} of {repository} does not point at {commit}")
    checkout = workdir / entry["id"]
    checkout.mkdir(parents=True)
    _run("git", "init", "-q", cwd=checkout)
    _run("git", "fetch", "-q", "--depth", "1", repository, commit, cwd=checkout)
    _run("git", "checkout", "-q", "FETCH_HEAD", cwd=checkout)
    return checkout / source.get("path", "")


def _icon_problems(directory: Path, icon: str) -> list[str]:
    path = directory / icon
    if not path.is_file():
        return [f"icon {icon} is missing"]
    data = path.read_bytes()
    problems = []
    if len(data) > ICON_MAX_BYTES:
        problems.append(f"icon {icon} is {len(data)} bytes; the limit is {ICON_MAX_BYTES}")
    text = data.decode("utf-8", errors="replace")
    lower = text.lower()
    if "<script" in lower or "javascript:" in lower or re.search(r"\son[a-z]+\s*=", lower):
        problems.append(f"icon {icon} contains script")
    if "<foreignobject" in lower or "<!entity" in lower or "<!doctype" in lower:
        problems.append(f"icon {icon} uses foreignObject or a DTD")
    if re.search(r"(?:href|src)\s*=\s*[\"'](?!#)", lower) or "url(http" in lower or "@import" in lower:
        problems.append(f"icon {icon} loads something from outside the file")
    view_box = re.search(r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)\s*"', text)
    if not view_box:
        problems.append(f"icon {icon} needs a viewBox")
    elif float(view_box.group(1)) != float(view_box.group(2)):
        problems.append(f"icon {icon} must be square, viewBox is {view_box.group(1)}x{view_box.group(2)}")
    return problems


def kit_problems(directory: Path, releases: Path = KIT_RELEASES) -> list[str]:
    """Every vendored copy of the adapter kit must match a release listed in kit/python/releases.json."""
    known = {digest: version for version, digest in json.loads(releases.read_text()).items()}
    problems = []
    for path in sorted(directory.rglob(KIT_FILE)):
        relative = path.relative_to(directory)
        if any(part in EXCLUDED for part in relative.parts):
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() not in known:
            problems.append(
                f"{relative} is not a released adapter kit; copy it unchanged from kit/python in {REGISTRY_URL}"
            )
    return problems


def load_adapter(entry: dict, directory: Path) -> Adapter:
    """Reads and checks the manifest, icon, entry point and README of one adapter."""
    manifest_path = directory / "adapter.toml"
    if not manifest_path.is_file():
        raise RegistryError(f"{entry['id']}: adapter.toml is missing")
    try:
        manifest = tomllib.loads(manifest_path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise RegistryError(f"{entry['id']}: adapter.toml: {error}") from error

    adapter = Adapter(entry=entry, manifest=manifest, directory=directory)
    adapter.problems += _schema_errors(_schema("adapter.schema.json"), manifest, f"{entry['id']}/adapter.toml")
    if adapter.problems:
        return adapter

    meta = adapter.meta
    if meta["id"] != entry["id"]:
        adapter.problems.append(f"{entry['id']}: adapter.toml id is {meta['id']}")
    adapter.problems += [f"{entry['id']}: {p}" for p in _icon_problems(directory, meta["icon"])]
    runtime = meta["runtime"]
    if runtime["kind"] == "python3" and not (directory / runtime["entry"]).is_file():
        adapter.problems.append(f"{entry['id']}: entry {runtime['entry']} is missing")
    if runtime["kind"] == "binary":
        adapter.problems.append(f"{entry['id']}: compiled adapters are built by build.yml, which is not enabled yet")
    readme = directory / "README.md"
    if not readme.is_file():
        adapter.problems.append(f"{entry['id']}: README.md is missing")
    else:
        text = readme.read_text()
        for section in REQUIRED_README_SECTIONS:
            if section not in text:
                adapter.problems.append(f"{entry['id']}: README.md needs a '{section}' section")
    for translated in sorted(directory.glob("README.*.md")):
        lang = translated.name[len("README."):-len(".md")]
        sections = TRANSLATED_README_SECTIONS.get(lang)
        if sections is None:
            adapter.problems.append(f"{entry['id']}: {translated.name} is in a language the store does not show yet")
            continue
        text = translated.read_text()
        for section in sections:
            if section not in text:
                adapter.problems.append(f"{entry['id']}: {translated.name} needs a '{section}' section")
    if not any((directory / name).is_file() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")) and entry["source"]["type"] == "git":
        adapter.problems.append(f"{entry['id']}: LICENSE file is missing")
    adapter.problems += _search_problems(entry["id"], manifest)
    adapter.problems += [f"{entry['id']}: {p}" for p in kit_problems(directory)]
    if not (directory / "tests").is_dir():
        adapter.problems.append(f"{entry['id']}: tests/ is missing")
    return adapter


def _search_problems(adapter_id: str, manifest: dict) -> list[str]:
    """The daemon refuses a [search] block that names a model or field the manifest lacks."""
    search = manifest.get("search") or {}
    model = (manifest.get("models") or {}).get(search.get("primary_model"))
    if model is None:
        return [f"{adapter_id}: search.primary_model {search.get('primary_model')!r} is not a model"]
    fields = set((model.get("fields") or {}).keys())
    problems = []
    for key in ("title", "preview", "subtitle", "date_field"):
        if key in search and search[key] not in fields:
            problems.append(f"{adapter_id}: search.{key} {search[key]!r} is not a field of {search['primary_model']}")
    for name in search.get("search_fields", []):
        if name not in fields and not name.startswith("_derived."):
            problems.append(f"{adapter_id}: search.search_fields {name!r} is not a field of {search['primary_model']}")
    return problems


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def version_problems(adapters: list[Adapter], previous: dict | None) -> list[str]:
    """An adapter whose source changed must raise its version; a version never goes back."""
    if not previous:
        return []
    published = {a["id"]: a for a in previous.get("adapters", [])}
    problems = []
    for adapter in adapters:
        before = published.get(adapter.id)
        if not before or adapter.problems:
            continue
        now, then = _version_tuple(adapter.meta["version"]), _version_tuple(before["version"])
        if now < then:
            problems.append(f"{adapter.id}: version {adapter.meta['version']} is lower than the published {before['version']}")
        elif now == then and (
            adapter.entry["source"] != before["source_entry"] or _archive_changed(adapter, before)
        ):
            problems.append(f"{adapter.id}: the source changed, so the version must increase from {before['version']}")
    return problems


def _archive_changed(adapter: Adapter, before: dict) -> bool:
    """The files differ from the published archive, so users of that version would not see them."""
    published = [a.get("sha256") for a in before.get("artifacts", []) if a.get("target") == "any"]
    if not published:
        return False
    return sha256(pack(adapter.directory, adapter.id)) != published[0]


def pack(directory: Path, prefix: str) -> bytes:
    """Reproducible .tar.gz of an adapter folder: sorted names, fixed owners, times and modes."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(directory.rglob("*")):
                relative = path.relative_to(directory)
                if any(part in EXCLUDED for part in relative.parts) or path.suffix == ".pyc":
                    continue
                info = tarfile.TarInfo(f"{prefix}/{relative.as_posix()}")
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                elif path.is_file():
                    data = path.read_bytes()
                    info.size = len(data)
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


PNG_SIZE = 256


def render_png(svg: bytes) -> bytes:
    """The icon as a 256 px PNG, for places that do not show SVG (Discord, link previews)."""
    result = subprocess.run(
        ["rsvg-convert", "--width", str(PNG_SIZE), "--height", str(PNG_SIZE), "--keep-aspect-ratio", "--format", "png"],
        input=svg,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        raise RegistryError(f"rsvg-convert could not render the icon: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_revoked(root: Path = ROOT) -> list[dict]:
    path = root / "revoked.json"
    return json.loads(path.read_text()) if path.is_file() else []


def collect(root: Path, workdir: Path) -> tuple[list[Adapter], list[str]]:
    """Every entry with its adapter, and the problems found on the way."""
    adapters, problems = [], []
    for path, entry in load_entries(root):
        entry_problems = validate_entry(path, entry)
        if entry_problems:
            problems += entry_problems
            continue
        try:
            adapter = load_adapter(entry, fetch_source(entry, root, workdir))
        except RegistryError as error:
            problems.append(str(error))
            continue
        adapters.append(adapter)
        problems += adapter.problems
    return adapters, problems


def build_index(root: Path, out: Path, sequence: int, base_url: str, workdir: Path) -> dict:
    """Writes archives, icons and index.json into `out` and returns the index."""
    adapters, problems = collect(root, workdir)
    if problems:
        raise RegistryError("\n".join(problems))
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for adapter in adapters:
        meta = adapter.meta
        archive_name = f"{adapter.id}-{meta['version']}.tar.gz"
        archive = pack(adapter.directory, adapter.id)
        (out / archive_name).write_bytes(archive)
        icon_name = f"{adapter.id}-{meta['version']}.svg"
        icon = (adapter.directory / meta["icon"]).read_bytes()
        (out / icon_name).write_bytes(icon)
        png_name = f"{adapter.id}-{meta['version']}.png"
        png = render_png(icon)
        (out / png_name).write_bytes(png)
        source = dict(adapter.entry["source"])
        if source["type"] == "local":
            source["commit"] = _run("git", "rev-parse", "HEAD", cwd=root).strip()
        items.append(
            {
                "id": adapter.id,
                "name": meta["name"],
                "summary": meta["summary"],
                "description": meta.get("description", meta["summary"]),
                "version": meta["version"],
                "author": meta["author"],
                "license": meta["license"],
                "homepage": meta.get("homepage", f"{REGISTRY_URL}/tree/main/{adapter.entry['source'].get('path', '')}"),
                "categories": meta.get("categories", []),
                "trust": adapter.entry["trust"],
                "contract": meta["contract"],
                "runtime": meta["runtime"],
                "platforms": meta["platforms"],
                "permissions": meta.get("permissions", {}),
                "source": source,
                "source_entry": adapter.entry["source"],
                "icon": {
                    "url": base_url + icon_name,
                    "sha256": sha256(icon),
                    "png": {"url": base_url + png_name, "sha256": sha256(png), "size": PNG_SIZE},
                },
                "i18n": meta.get("i18n", {}),
                "artifacts": [{"target": "any", "url": base_url + archive_name, "sha256": sha256(archive), "size": len(archive)}],
            }
        )
    index = {
        "format": 1,
        "sequence": sequence,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "registry": REGISTRY_URL,
        "adapters": items,
        "revoked": load_revoked(root),
    }
    schema_problems = _schema_errors(_schema("index.schema.json"), index, "index.json")
    if schema_problems:
        raise RegistryError("\n".join(schema_problems))
    (out / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    return index


def validate(root: Path = ROOT, previous: dict | None = None) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        adapters, problems = collect(root, Path(tmp))
        for adapter in adapters:
            icon = adapter.directory / adapter.meta.get("icon", "")
            if icon.is_file():
                try:
                    render_png(icon.read_bytes())
                except RegistryError as error:
                    problems.append(f"{adapter.id}: {error}")
    return problems + version_problems(adapters, previous)


# ── Review report ───────────────────────────────────────────────────────────

PERMISSION_KINDS = ("read", "network", "secrets", "tools")


def _permission_lines(permissions: dict, before: dict | None) -> list[str]:
    lines = []
    for kind in PERMISSION_KINDS:
        values = permissions.get(kind, [])
        old = set((before or {}).get(kind, []))
        if not values and not old:
            continue
        shown = [f"**`{v}`** (new)" if before is not None and v not in old else f"`{v}`" for v in values]
        dropped = [f"~~`{v}`~~" for v in sorted(old - set(values))]
        lines.append(f"- {kind}: " + (", ".join(shown + dropped) or "none"))
    return lines or ["- none: no files, network, secrets or programs"]


def report(root: Path, previous: dict | None, workdir: Path) -> str:
    """Markdown for the pull request: what each changed adapter is and may access."""
    adapters, problems = collect(root, workdir)
    problems += version_problems(adapters, previous)
    published = {item["id"]: item for item in (previous or {}).get("adapters", [])}
    changed = [
        a for a in adapters
        if a.id not in published
        or published[a.id].get("version") != a.meta.get("version")
        or published[a.id].get("source_entry") != a.entry.get("source")
    ]
    out = ["<!-- wingbot-review -->", "## Wingbot review", ""]
    if problems:
        out += ["**Fails the registry checks.** Fix these before review:", ""]
        out += [f"- {p}" for p in problems] + [""]
    else:
        out += ["Registry checks pass. A maintainer still reads the code before merging.", ""]
    if not changed:
        out.append("No adapter is added or updated by this pull request.")
    for adapter in changed:
        meta = adapter.meta
        before = published.get(adapter.id)
        version = f"{before['version']} → {meta['version']}" if before else f"{meta['version']} (new)"
        runtime = meta.get("runtime", {})
        out += [
            f"### {meta.get('name', adapter.id)} `{adapter.id}` {version}",
            "",
            f"> {meta.get('summary', '')}",
            "",
            f"- trust: `{adapter.entry.get('trust')}`, source: `{adapter.entry['source'].get('type')}`"
            + (f" {adapter.entry['source'].get('repository')}@`{adapter.entry['source'].get('commit', '')[:12]}`"
               if adapter.entry["source"].get("type") == "git" else ""),
            f"- runtime: `{runtime.get('kind')}` `{runtime.get('entry')}`, platforms: {', '.join(meta.get('platforms', []))}",
            "",
            "**Permissions**" + (" (compared with the published version)" if before else ""),
            "",
        ]
        out += _permission_lines(meta.get("permissions", {}), (before or {}).get("permissions") if before else None)
        files = sorted(str(p.relative_to(adapter.directory)) for p in adapter.directory.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts)
        out += ["", f"<details><summary>{len(files)} files</summary>", "", *[f"- `{f}`" for f in files], "", "</details>", ""]
    return "\n".join(out).rstrip() + "\n"


# ── Announcements ───────────────────────────────────────────────────────────

WINGBOT_AVATAR = "https://raw.githubusercontent.com/bernardopg/wingdrive-adapters/main/assets/wingbot.png"
TRUST_LABELS = {"official": "Official", "verified": "Verified", "community": "Community"}


def announcement(previous: dict | None, current: dict) -> dict | None:
    """A Discord webhook message for adapters added, updated or revoked since `previous`, or None."""
    before = {item["id"]: item for item in (previous or {}).get("adapters", [])}
    embeds = []
    for item in current.get("adapters", []):
        old = before.get(item["id"])
        if old and old.get("version") == item["version"]:
            continue
        title = f"{item['name']} {item['version']}" + (" is new in the store" if not old else f" (was {old['version']})")
        permissions = item.get("permissions", {})
        access = "; ".join(f"{kind}: {', '.join(permissions[kind])}" for kind in PERMISSION_KINDS if permissions.get(kind))
        png = (item.get("icon") or {}).get("png")
        embed = {
            **({"thumbnail": {"url": png["url"]}} if png else {}),
            "title": title[:256],
            "url": item.get("homepage") or REGISTRY_URL,
            "description": item.get("summary", "")[:400],
            "color": 0x7C5CFF if not old else 0x3B82F6,
            "author": {"name": item.get("author", "")} if item.get("author") else None,
            "footer": {"text": f"Signed index {current.get('sequence')} · wingdrive.org/adapters"},
            "fields": [
                {"name": "Trust", "value": TRUST_LABELS.get(item.get("trust"), item.get("trust", "")), "inline": True},
                {"name": "Install", "value": f"`wing adapters install {item['id']}`", "inline": True},
                {"name": "Access", "value": (access or "none: works offline")[:1024], "inline": False},
            ],
        }
        embeds.append({key: value for key, value in embed.items() if value is not None})
    old_revoked = {(r["id"], v) for r in (previous or {}).get("revoked", []) for v in r.get("versions", [])}
    for revocation in current.get("revoked", []):
        versions = [v for v in revocation.get("versions", []) if (revocation["id"], v) not in old_revoked]
        if versions:
            embeds.append({
                "title": f"{revocation['id']} {', '.join(versions)} revoked"[:256],
                "description": (
                    "WingDrive disables this version on every install. Reason: " + revocation.get("reason", "")
                )[:400],
                "color": 0xEF4444,
                "footer": {"text": f"Signed index {current.get('sequence')}"},
            })
    if not embeds:
        return None
    return {
        "username": "Wingbot",
        "avatar_url": WINGBOT_AVATAR,
        "content": f"Adapter store index {current.get('sequence')}",
        "embeds": embeds[:10],
        "allowed_mentions": {"parse": []},
    }
