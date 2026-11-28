# Coding agent sessions

Search the sessions you had with coding agents next to your files: what you asked, what the agent
answered, the tools it ran and the files it edited. Reads the transcripts that **Claude Code** and
**Codex** keep on this computer.

## Setup

1. Install the adapter from **Sources > Add source > Adapters**.
2. Add a source. The folders are filled in when Claude Code (`~/.claude/projects`) or Codex
   (`~/.codex/sessions`) is found; leave a folder empty to use its default, or pick another one.

An agent that is not installed is skipped. The first sync reads every transcript, which takes
about half a minute for a thousand sessions; later syncs read only the sessions that changed.

## Data

One record per session:

| Field | From |
|---|---|
| `title` | The title you gave the session in Claude Code, or the first prompt |
| `body` | Your prompts and the agent's answers, up to 200,000 characters |
| `agent` | `Claude Code` or `Codex` |
| `project` | The folder the session ran in |
| `branch` | The Git branch, when the agent recorded it |
| `started`, `updated` | The first and last message |
| `prompts` | How many prompts you typed |
| `tools` | The tools the agent called, most used first |
| `files` | The files it edited |

Left out: tool output, the model's reasoning, subagent transcripts, and text the agents add to your
turns themselves (reminders, environment context, slash command output). A deleted transcript
removes its session at the next sync.

## Privacy

Transcripts often hold code, paths and credentials. Before anything is stored, the adapter replaces
what looks like a secret with `[secret]`: API keys (Anthropic, OpenAI, GitHub, GitLab, Slack, AWS,
Google), JSON web tokens, private keys, and values written as `password=`, `token:` or `api_key =`.
This is a best effort: a password typed in plain words is kept as it is.

The adapter reads only the two folders, needs no network and no account. Nothing leaves your
computer.
