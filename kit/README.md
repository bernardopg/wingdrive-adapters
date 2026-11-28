# Adapter kit

Helpers for writing WingDrive adapters. Each kit is one file that you copy into your adapter's
folder. The daemon runs adapters with `python3 -I`, only the standard library and no installed
packages, so a package from PyPI would not be found.

| Path | Language |
|---|---|
| `python/wingdrive_adapter.py` | Python 3.9 or later |

## Python

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python3 -I` leaves the folder off sys.path

from wingdrive_adapter import Adapter, InputError, run

def sync(adapter: Adapter) -> None:
    folder = adapter.path("folder")              # config field, `~` expanded, must be a folder
    token = adapter.secret("token")              # secret field; replaced by [secret] in logs and errors
    known = adapter.cursor(version=1)            # {} on the first sync or after a format change
    adapter.upsert("note", "a.txt", title="a")   # one record
    adapter.delete("note", "gone.txt")           # the daemon never notices deletions on its own
    adapter.save_cursor({"files": ...}, version=1)  # last, after every record it covers

def detect(adapter: Adapter) -> None:            # optional, for `runtime.detect = true`
    adapter.suggest("folder", ["/home/ana/Notes"])

if __name__ == "__main__":
    run(sync, detect)
```

| Part | What it does |
|---|---|
| `get`, `require`, `flag`, `number`, `items`, `path` | Read config fields, with empty strings treated as missing |
| `secret` | Read a `secret = true` field and keep its value out of every log line and error |
| `cursor`, `save_cursor` | JSON cursor with a version, so a new format starts a full sync |
| `upsert`, `delete`, `link`, `unlink`, `suggest`, `log`, `info`, `warn` | Print contract lines |
| `confined` | True inside the Linux sandbox, where undeclared paths do not exist |
| `data_dir` | The folder only this source's adapter writes to, or `None` in detect mode |
| `config_home()`, `data_home()` | Where other desktop apps keep settings and data, per platform |
| `run` | Reads stdin, calls `sync` or `detect`, exits 0, or 2 with the message for an `InputError` |

`templates/python/` is a complete adapter built on the kit.

## Releases

`python/releases.json` lists the SHA-256 of every released `wingdrive_adapter.py`. The registry
accepts a vendored copy only when it matches one of them, so reviewers read the kit's code once.
To change the kit, raise `KIT_VERSION`, add the new hash to `releases.json` and copy the file to
`templates/python/`. The tests check all three.
