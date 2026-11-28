# Text notes

A template for WingDrive adapters written in Python. It makes one note for each `.txt` file at the
top of a folder. Copy this folder to start your own adapter.

## Setup

1. Install the adapter, then open **Sources > Add source** in WingDrive and pick **Text notes**.
2. Choose the folder that holds your `.txt` files.

The first sync reads every file. Later syncs read only the files that changed.

## Data

Each note keeps:

| Field | From |
|---|---|
| `title` | the file name without `.txt` |
| `body` | the text, up to 200,000 characters |
| `words` | the word count |
| `modified` | the file's modification time |

Files in subfolders and files with other extensions are skipped. When you delete a file, its note
is removed at the next sync.

## Privacy

The adapter reads only the folder you choose. It needs no network and no account. Nothing leaves
your computer.

## Make it yours

| File | What to change |
|---|---|
| `adapter.toml` | `id`, `name`, texts, `version`, `author`, `homepage`, permissions, config and models |
| `sync.py` | What a sync reads and the records it prints |
| `wingdrive_adapter.py` | Nothing. This is the adapter kit; replace it with a newer release when one comes out |
| `fixtures/<case>/` | One folder per case for `wing adapter test`: `config.json`, `expected.json` and any data files |
| `tests/` | Unit tests for your own code |

Check the adapter the way the daemon and the registry run it:

```bash
wing adapter test .                     # every fixtures/<case>
python3 -m unittest discover -s tests -t tests
```

`config.json` is the config the user would fill. `{fixture}` in it becomes the case's folder.
`expected.json` lists the record counts, records and fields to find after two syncs in a row,
which must leave the same records. A case can expect an `"error"` instead. The format is
described in `docs/core/adapters.mdx`, section "Testing", in the WingDrive repository.

The kit's functions are documented in `wingdrive_adapter.py`. It needs Python 3.9 or later and
only the standard library, which is all the daemon gives an adapter.
