# Drafts

Unfinished adapters kept for later work. Nothing here is published or listed in the index.

## Prototypes from the app repository

These nine prototypes lived in `adapters/<id>/` of
[`bernardopg/wingdrive`](https://github.com/bernardopg/wingdrive) until ADAPT-015. They were copied
unchanged from commit
[`93eb1dd`](https://github.com/bernardopg/wingdrive/tree/93eb1dd50284d494bac3727b27718dbba324ad10/adapters)
of that repository, where their history stays.

They predate contract 1 (`docs/core/adapters.mdx` in the app repository): their manifests have no
`contract`, `platforms` or `permissions`, and the scripts read the old input format. To finish one,
start from `templates/python/`, port the parsing, then move it to `adapters/<id>/` with fixtures,
tests and a registry entry.

| Draft | What it reads | Task in the app repository |
|---|---|---|
| `apple-notes` | Notes from the Apple Notes database (macOS) | |
| `chrome-bookmarks` | Chrome bookmarks | |
| `chrome-history` | Chrome browsing history | |
| `gmail` | Mail, threads and labels through the Gmail API | |
| `macos-calendar` | Calendar events (macOS) | |
| `macos-contacts` | Contacts (macOS) | |
| `opencode` | OpenCode coding sessions | ADAPT-014 |
| `safari-history` | Safari browsing history (macOS) | |
| `slack` | A Slack workspace export (JSON) | |

The prototypes of `obsidian` and `github` were finished as `adapters/obsidian/` and
`adapters/github/` here, so they are not kept as drafts.
