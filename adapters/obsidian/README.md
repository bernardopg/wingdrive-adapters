# Obsidian

Brings the notes of an Obsidian vault into a WingDrive library, so they show up in search next to
your files, with their tags, aliases and the links between them. Works with any folder of Markdown
files, not only Obsidian vaults.

## Setup

1. In WingDrive, open **Sources > Add source > Adapters** and install **Obsidian**, or run
   `wing adapters install obsidian`.
2. Add a source. The vault you opened last in Obsidian is filled in, and the others are offered;
   any folder of Markdown files works too. Obsidian's list of vaults is read on Linux (native,
   Flatpak and Snap), macOS (`~/Library/Application Support/obsidian`, iCloud vaults included) and
   Windows (`%APPDATA%\obsidian`).
3. Optionally list folders or files to skip, separated by commas: `Templates, Archive/*, *.excalidraw.md`.

The adapter needs Python 3.9 or later (`python3` on Linux and macOS, `python` on Windows). It uses
only the standard library.

## Data

One record per Markdown note:

| Field | From |
|---|---|
| `title` | `title` in the frontmatter, or the file name |
| `body` | The note without its frontmatter and without a first heading that repeats the title (first 200,000 characters) |
| `path`, `folder` | The path inside the vault |
| `tags` | Frontmatter `tags` and inline `#tags`, outside code blocks |
| `aliases` | Frontmatter `aliases` |
| `created` | Frontmatter `created` or `date`, else the file's creation or modification time |
| `modified` | The file's modification time |
| `word_count` | Words outside code blocks and comments |

Notes link to the notes they point at with `[[wikilinks]]` (including `[[Note#Heading]]`,
`[[Note|text]]` and `![[Note]]` embeds) and `[text](Note.md)` links. Links resolve like Obsidian:
by path, then by file name (the shortest path wins), then by alias.

Skipped: hidden folders and files (`.obsidian`, `.trash`, `.git`), `node_modules`, symbolic links,
and the patterns you list.

The first sync reads every note. Later syncs read only notes whose size or modification time
changed, delete the records of notes that are gone, and update links, including links whose target
note was created after them.

## Privacy

The adapter reads only the vault folder you choose, plus Obsidian's own list of vaults
(`obsidian.json`) to suggest one. It does not use the network, run other
programs, or write anywhere. Your notes stay in your WingDrive library on this computer.
