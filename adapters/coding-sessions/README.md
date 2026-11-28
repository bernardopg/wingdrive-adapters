# Coding agent sessions

Search the sessions you had with coding agents next to your files: what you asked, what the agent
answered, the tools it ran and the files it edited. Reads the sessions that **Claude Code**,
**Codex** and **OpenCode** keep on this computer.

## Setup

1. Install the adapter from **Sources > Add source > Adapters**.
2. Add a source. The fields are filled in when Claude Code (`~/.claude/projects`), Codex
   (`~/.codex/sessions`) or OpenCode (`~/.local/share/opencode/opencode.db`, on macOS and Windows
   too) is found; leave a field empty to use its default, or pick another location.

An agent that is not installed is skipped. The first sync reads every session, which takes about
half a minute for a thousand sessions. Later syncs read only what changed: the lines added to a
Claude Code or Codex transcript since the last sync, and the OpenCode sessions with new messages.

## Data

One record per session:

| Field | From |
|---|---|
| `title` | The title the session has in the agent, or the first prompt |
| `summary` | The first prompt and the last answer, up to 400 characters each; shown as the preview |
| `body` | All your prompts and the agent's answers, in full; this is what search reads |
| `agent` | `Claude Code`, `Codex` or `OpenCode` |
| `project` | The folder the session ran in |
| `branch` | The Git branch, when the agent recorded it (OpenCode does not) |
| `started`, `updated` | The first and last message |
| `prompts` | How many prompts you typed |
| `tools` | The tools the agent called, most used first |
| `files` | The files it edited, with relative paths joined to the session's folder |

Each tool (`tool` records, by name) and each edited file (`file` records, by path) is also a record,
linked to the sessions that used it. A tool or file no session uses any more is removed.

Left out: tool output, the model's reasoning, subagent sessions, and text the agents add to your
turns themselves (reminders, environment context, slash command output). A deleted transcript or
OpenCode session removes its record at the next sync.

To resume a transcript where the last sync stopped, the adapter keeps what it read from each one,
redacted and compressed, in the source's own state folder inside WingDrive's data folder. A
transcript that shrank or changed before that point is read again from the start.

## Privacy

Transcripts often hold code, paths and credentials. Before anything is stored, the adapter replaces
what looks like a secret with `[secret]`: API keys (Anthropic, OpenAI, GitHub, GitLab, Slack, AWS,
Google), JSON web tokens, private keys, and values written as `password=`, `token:` or `api_key =`.
This is a best effort: a password typed in plain words is kept as it is.

The adapter reads only the two folders and OpenCode's database file (opened read only; OpenCode's
`auth.json` and the rest of its folder stay out of reach), needs no network and no account.
Nothing leaves your computer.
