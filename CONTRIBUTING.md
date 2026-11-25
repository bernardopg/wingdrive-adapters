# Publishing an adapter

## Before you open a pull request

- Your adapter lives in a public GitHub repository with a license, a README and tests.
- `adapter.toml` declares contract 1 and every permission the adapter uses: paths it reads,
  network hosts it contacts, secrets it needs and tools it runs. Undeclared access fails review and
  is blocked at run time. A `read` path can name a config field, `"{config.vault_path}"`, when
  the adapter reads the folder the user picks there.
- `adapter.toml` speaks the contract in
  [`docs/core/adapters.mdx`](https://github.com/bernardopg/wingdrive/blob/main/docs/core/adapters.mdx):
  config and cursor on stdin, JSON lines on stdout, deletions printed by the adapter.
- `[search]` names the primary model and fields of it for `title`, `preview` and `search_fields`.
- `README.md` has `## Setup`, `## Data` and `## Privacy` sections.
- The icon is a square SVG under 64 KB, without scripts.
- The summary fits in two lines (140 characters).
- You tagged the commit you want published.

## The entry

Add `registry/<id>.json`:

```json
{
  "id": "foo",
  "trust": "community",
  "source": {
    "type": "git",
    "repository": "https://github.com/alice/wingdrive-adapter-foo",
    "tag": "v1.0.0",
    "commit": "0123456789abcdef0123456789abcdef01234567"
  },
  "maintainers": ["alice"]
}
```

Everything else (name, summary, version, permissions) is read from the `adapter.toml` at that
commit. The tag must keep pointing at that commit.

## Updates

Open a pull request that moves `tag` and `commit` and raise `version` in `adapter.toml`. The
review shows the diff since the last published commit. An update that asks for new permissions
makes the app ask every user again before installing it.

## Review

The Validate workflow runs on every push to your pull request. A maintainer then reads the code and
the report. Reasons for rejection include undeclared network or file access, collecting data the
adapter does not need for its purpose, obfuscated code, and binaries committed to the repository.

## Trust levels

| Level | Meaning |
|---|---|
| `official` | Kept in `adapters/` of this repository by the WingDrive maintainers |
| `verified` | Authors with two or more approved adapters and no revocations |
| `community` | Everyone else |
