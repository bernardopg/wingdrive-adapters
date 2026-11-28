# WingDrive Adapters

The registry of the [WingDrive](https://github.com/bernardopg/wingdrive) adapter store.

Adapters bring data from other apps and services into a WingDrive library: notes, mail, code,
chat, browser history. The WingDrive app installs them from the store in **Sources > Add source >
Adapters**, and from the command line with `wing adapters install <id>`.

This repository holds:

| Path | What |
|---|---|
| `registry/` | One entry per published adapter: where it comes from and how far it is trusted |
| `adapters/` | Official adapters, kept here and reviewed like any other |
| `templates/` | Starting points for new adapters (Python now, Rust later) |
| `kit/` | The adapter kit that adapters copy into their folder (`kit/python/wingdrive_adapter.py`) |
| `drafts/` | Unfinished adapters; never published |
| `schema/` | JSON Schemas for entries, `adapter.toml` and the index |
| `keys/` | Public keys that sign the index, and the rotation log |
| `revoked.json` | Adapter versions the app must disable |
| `tools/wingreg/` | The validator and index builder the workflows run |

How the store works end to end, with diagrams:
[`docs/design/adapter-store.md`](https://github.com/bernardopg/wingdrive/blob/main/docs/design/adapter-store.md)
in the app repository.

## How publishing works

1. A pull request adds or updates `registry/<id>.json` (and, for official adapters, `adapters/<id>/`).
2. The **Validate** workflow checks the entry, the `adapter.toml`, the icon, the README and the
   version, without any secret. [Wingbot](https://github.com/apps/wingdrive-wingbot) comments the
   result on the pull request and labels it `ready-for-review` or `needs-changes`.
3. A maintainer reviews the code and the report, then merges.
4. The **Publish** workflow builds a reproducible archive for each adapter, writes `index.json`,
   signs it with the registry key after the maintainer approves the `registry-publish`
   environment, and releases it as `index-<sequence>`.
5. Apps fetch `https://github.com/bernardopg/wingdrive-adapters/releases/latest/download/index.json`
   and its `.minisig`, verify the signature with the keys they ship, and install the archives whose
   SHA-256 matches the index.

## Publish your adapter

Read [CONTRIBUTING.md](CONTRIBUTING.md). Adapters are processes that speak JSON lines on stdin and
stdout, so they can be written in Python or compiled from Rust, Go or any other language. Compiled
adapters are always built by this repository's CI from the reviewed commit.

## Check locally

```bash
uv run --python 3.12 --with jsonschema python -m unittest discover -s tests
cd tools && uv run --python 3.12 --with jsonschema python -m wingreg validate --run-code
```

`--run-code` also runs each adapter's tests and the contract harness, as Wingbot does. It needs
`wing-adapter-test` (or a `wing` with `wing adapter test`) on `PATH`, or its path in
`WING_ADAPTER_TEST`; build it in a WingDrive checkout with
`cargo build --locked -p wing-archive --bin wing-adapter-test`. Without `--run-code` only the static
checks run.

## License

Apache-2.0 for the registry tooling. Each adapter states its own license in its `adapter.toml`.
