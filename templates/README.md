# Templates

Starting points for new adapters. Copy one, rename it and follow its README. Each one ships the
Apache-2.0 `LICENSE` its `adapter.toml` names; change both if you pick another license. Run
`wing adapter validate` in the copy to get the checks Wingbot runs on a pull request.

| Template | Language | What it shows |
|---|---|---|
| `python/` | Python 3.9+ | One note per `.txt` file: config, incremental syncs with a cursor, deletions, errors, fixtures for `wing adapter test` and unit tests |

The Python template uses the adapter kit in `kit/python/`. A Rust template arrives with ADAPT-008
in the WingDrive tasks.
