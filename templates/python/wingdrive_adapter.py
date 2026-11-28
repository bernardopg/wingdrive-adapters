"""WingDrive adapter kit for Python (contract 1).

Copy this file next to your adapter's entry script and import it:

    from wingdrive_adapter import Adapter, InputError, run

    def sync(adapter):
        folder = adapter.path("folder")
        state = adapter.cursor(version=1)
        for name in sorted(os.listdir(folder)):
            adapter.upsert("note", name, title=name)
        adapter.save_cursor({"seen": ...}, version=1)

    if __name__ == "__main__":
        run(sync)

The daemon runs adapters with `python3 -I`, so only the standard library is available and nothing
is installed from PyPI; that is why the kit is one file you vendor, not a package. It works on
Python 3.9 and later and uses only the standard library.

The contract it speaks is documented in docs/core/adapters.mdx in the WingDrive repository:
https://github.com/bernardopg/wingdrive/blob/main/docs/core/adapters.mdx
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

__all__ = ["Adapter", "InputError", "run", "config_home", "data_home", "KIT_VERSION"]

#: Version of this file. The registry checks that vendored copies match a released kit.
KIT_VERSION = "1.0.0"

CONTRACT = 1
LOG_LEVELS = ("debug", "info", "warn", "error")
REDACTED = "[secret]"


class InputError(Exception):
    """A problem the user can fix, such as a missing folder or a wrong token.

    `run` prints the message to stderr and exits with status 2; the app shows it as the reason the
    sync failed. Secret values in the message are replaced before it is printed.
    """


class Adapter:
    """One run of an adapter: what the daemon sent on stdin, and the lines it prints back."""

    def __init__(self, payload: Dict[str, Any], out=None, environ: Optional[Dict[str, str]] = None):
        if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
            raise InputError(f"expected a contract {CONTRACT} input document on stdin")
        config = payload.get("config") or {}
        if not isinstance(config, dict):
            raise InputError("config must be a JSON object")
        self.config: Dict[str, Any] = config
        self.mode: str = payload.get("mode") or "sync"
        raw_cursor = payload.get("cursor")
        self.raw_cursor: Optional[str] = raw_cursor if isinstance(raw_cursor, str) else None
        data_dir = payload.get("data_dir")
        self.data_dir: Optional[Path] = Path(data_dir) if data_dir else None
        self._out = out if out is not None else sys.stdout
        self._environ = environ if environ is not None else os.environ
        self._secrets: List[str] = []

    @classmethod
    def from_stdin(cls) -> "Adapter":
        """Read the input document the daemon writes to stdin."""
        try:
            payload = json.load(sys.stdin)
        except ValueError as error:
            raise InputError(f"stdin is not JSON: {error}") from None
        return cls(payload)

    # ── Config ──────────────────────────────────────────────────────────────

    @property
    def detecting(self) -> bool:
        """True when the daemon asks for suggestions for the add source form, not for a sync."""
        return self.mode == "detect"

    def get(self, key: str, default: Any = None) -> Any:
        """A config value, or `default` when the field is missing or empty."""
        value = self.config.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return value.strip() if isinstance(value, str) else value

    def require(self, key: str) -> Any:
        """A config value the adapter cannot run without."""
        value = self.get(key)
        if value is None:
            raise InputError(f"{key} is required")
        return value

    def flag(self, key: str, default: bool = False) -> bool:
        """A boolean field. Accepts true/false and the strings "true", "yes", "1", "on"."""
        value = self.config.get(key)
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in ("true", "yes", "1", "on")

    def number(self, key: str, default: Optional[int] = None) -> Optional[int]:
        """An integer field; strings such as "50" are accepted."""
        value = self.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise InputError(f"{key} must be a whole number, got {value!r}") from None

    def items(self, key: str) -> List[str]:
        """A comma-separated or list field as a list of non-empty strings."""
        value = self.get(key)
        if value is None:
            return []
        parts = value if isinstance(value, list) else str(value).split(",")
        return [str(part).strip() for part in parts if str(part).strip()]

    def path(self, key: str, must_be: Optional[str] = "folder", default: Optional[str] = None) -> Path:
        """A path field with `~` expanded. `must_be` is "folder", "file" or None to skip the check."""
        value = self.get(key, default)
        if value is None:
            raise InputError(f"{key} is required")
        path = Path(os.path.expanduser(str(value)))
        if must_be == "folder" and not path.is_dir():
            raise InputError(f"{path} is not a folder")
        if must_be == "file" and not path.is_file():
            raise InputError(f"{path} is not a file")
        return path

    def secret(self, key: str, required: bool = True) -> Optional[str]:
        """A field declared with `secret = true`, such as a token.

        The daemon adds it to `config` only for the run. The kit remembers the value and replaces it
        with "[secret]" in every log line and error message the adapter prints through the kit.
        """
        value = self.get(key)
        if value is None:
            if required:
                raise InputError(f"{key} is required")
            return None
        value = str(value)
        if value not in self._secrets:
            self._secrets.append(value)
        return value

    # ── Cursor ──────────────────────────────────────────────────────────────

    def cursor(self, version: int = 1) -> Dict[str, Any]:
        """The state saved by the last sync, or {} on the first sync.

        The kit stores the cursor as JSON with a `"v"` key. A cursor from another `version`, or one
        that is not a JSON object, reads as {} so a format change starts a full sync.
        """
        if not self.raw_cursor:
            return {}
        try:
            state = json.loads(self.raw_cursor)
        except ValueError:
            return {}
        if not isinstance(state, dict) or state.get("v") != version:
            return {}
        state = dict(state)
        state.pop("v")
        return state

    def save_cursor(self, state: Dict[str, Any], version: int = 1) -> None:
        """Store the state the next sync receives. Print it last, after every record it covers."""
        payload = {"v": version}
        payload.update(state)
        self.emit({"cursor": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)})

    # ── Output ──────────────────────────────────────────────────────────────

    def emit(self, line: Dict[str, Any]) -> None:
        """Print one contract line. Prefer the helpers below."""
        self._out.write(json.dumps(line, ensure_ascii=False) + "\n")

    def upsert(self, model: str, external_id: Any, fields: Optional[Dict[str, Any]] = None, **more: Any) -> None:
        """Insert or update a record. `external_id` must survive a restart (a path, a message id)."""
        values = dict(fields or {})
        values.update(more)
        self.emit({"upsert": model, "external_id": str(external_id), "fields": values})

    def delete(self, model: str, external_id: Any) -> None:
        """Delete a record and its links. The daemon never notices on its own that one is gone."""
        self.emit({"delete": model, "external_id": str(external_id)})

    def link(self, model: str, external_id: Any, to: str, to_id: Any) -> None:
        """Add a `many_to_many` link. Both records must have been upserted."""
        self.emit({"link": model, "id": str(external_id), "to": to, "to_id": str(to_id)})

    def unlink(self, model: str, external_id: Any, to: str, to_id: Any) -> None:
        """Remove a `many_to_many` link."""
        self.emit({"unlink": model, "id": str(external_id), "to": to, "to_id": str(to_id)})

    def suggest(self, key: str, values: Iterable[str]) -> None:
        """In detect mode, offer values for a config field, best first. Never suggest a secret."""
        unique: List[str] = []
        for value in values:
            if value and value not in unique:
                unique.append(value)
        if unique:
            self.emit({"suggest": {key: unique}})

    def log(self, level: str, message: str) -> None:
        """Log through the daemon at debug, info, warn or error. Secret values are replaced."""
        if level not in LOG_LEVELS:
            raise ValueError(f"log level must be one of {', '.join(LOG_LEVELS)}")
        self.emit({"log": level, "message": self.redact(message)})

    def info(self, message: str) -> None:
        self.log("info", message)

    def warn(self, message: str) -> None:
        self.log("warn", message)

    def redact(self, text: str) -> str:
        """`text` with every secret value read through `secret()` replaced by "[secret]"."""
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        return text

    # ── Environment ─────────────────────────────────────────────────────────

    @property
    def confined(self) -> bool:
        """True when the daemon runs the adapter in its Linux sandbox (bubblewrap).

        In the sandbox, paths the manifest does not declare do not exist and there is no network
        without declared hosts, so an adapter can explain a missing file instead of failing.
        """
        return self._environ.get("WINGDRIVE_ADAPTER_ISOLATION") == "bubblewrap"


# ── Platform paths ──────────────────────────────────────────────────────────


def config_home(environ: Optional[Dict[str, str]] = None, platform: Optional[str] = None) -> Path:
    """Where desktop apps keep their settings: the folder an app's own config file lives in.

    Linux: `$XDG_CONFIG_HOME` or `~/.config`. macOS: `~/Library/Application Support`.
    Windows: `%APPDATA%`. Use it in detect mode to find what another app knows, and declare the
    file in the manifest's `read` permissions.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path(environ.get("HOME") or environ.get("USERPROFILE") or os.path.expanduser("~"))
    if platform.startswith("win"):
        return Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
    if platform == "darwin":
        return home / "Library" / "Application Support"
    return Path(environ.get("XDG_CONFIG_HOME") or home / ".config")


def data_home(environ: Optional[Dict[str, str]] = None, platform: Optional[str] = None) -> Path:
    """Where desktop apps keep their data (databases, caches they own).

    Linux: `$XDG_DATA_HOME` or `~/.local/share`. macOS: `~/Library/Application Support`.
    Windows: `%LOCALAPPDATA%`.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path(environ.get("HOME") or environ.get("USERPROFILE") or os.path.expanduser("~"))
    if platform.startswith("win"):
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if platform == "darwin":
        return home / "Library" / "Application Support"
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")


# ── Entry point ─────────────────────────────────────────────────────────────


def run(sync: Callable[[Adapter], None], detect: Optional[Callable[[Adapter], None]] = None) -> None:
    """Read stdin, call `sync` (or `detect` in detect mode) and exit the process as the contract expects.

    Exits with status 0 when the function returns, 2 with the message on stderr for an `InputError`.
    Any other exception propagates, so its traceback reaches stderr and the sync fails.
    """
    # The daemon speaks UTF-8 on every platform; Windows consoles default to a code page.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    adapter = None
    try:
        adapter = Adapter.from_stdin()
        if adapter.detecting:
            if detect is not None:
                detect(adapter)
        else:
            sync(adapter)
    except InputError as error:
        message = adapter.redact(str(error)) if adapter is not None else str(error)
        sys.stdout.flush()
        print(message, file=sys.stderr)
        sys.exit(2)
    sys.stdout.flush()
    sys.exit(0)
