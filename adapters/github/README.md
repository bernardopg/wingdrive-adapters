# GitHub

Brings a GitHub account into a WingDrive library, so repositories, stars, issues and pull requests
show up in search next to your files.

## Setup

1. In WingDrive, open **Sources > Add source > Adapters** and install **GitHub**, or run
   `wing adapters install github`.
2. Add a source. The user name is filled in when the GitHub CLI (`gh`) is signed in on this
   computer; otherwise type it.
3. Optionally paste an access token to add private repositories, issues and pull requests:
   - a **fine-grained token** (Settings > Developer settings > Fine-grained tokens) with read
     access to *Metadata* and *Issues* (and *Pull requests*) on the repositories you want, or
   - a **classic token** with the `repo` scope, or the output of `gh auth token`.

The adapter needs Python 3.9 or later and uses only the standard library.

## Data

One record per item, with `kind` telling them apart:

| Kind | What | Needs a token |
|---|---|---|
| `repository` | Repositories you own (`relation` = `owner`) and starred (`starred`), with description, topics, language and stars | Public ones: no. Private ones: yes |
| `issue` | Issues you opened, were assigned or mentioned in, or subscribed to | Yes |
| `pull_request` | The same for pull requests; merged ones have `state` = `merged` | Yes |

Each record has `title`, `body`, `repo`, `number`, `state`, `author`, `labels`, `url`, `created`
and `updated`. Issues and pull requests also have `comments`: the text of their comments, oldest
first, so search finds an issue by what was said in it.

With a token, each comment is also a `comment` record that belongs to its issue or pull request,
with `body`, `author`, `url`, `created` and `updated`. Turn this off with **Comments** to save
requests. Review comments on lines of a pull request's diff are not included.

Every sync lists your repositories again (one request per 100) and drops the ones you deleted or
unstarred. Issues and pull requests come incrementally: only the ones updated since the last sync.
An issue that changed has its comments listed again (one request per 100 comments), which is how
edited and deleted comments are noticed. Without a token GitHub allows 60 requests an hour, with
one 5,000; when the limit is reached the sync stops and resumes on the next run. The first sync of
an account with thousands of issues lists comments for at most 10 minutes and continues on the
next syncs. A source added before 1.1.0 stores the comments but its search index only reads them
after the source is added again.

## Privacy

The adapter talks only to `api.github.com`, with the token you gave it, and only reads: it never
creates, changes or deletes anything on GitHub. To suggest your user name it reads the `user:` line
of the GitHub CLI's `hosts.yml`; it never reads the CLI's token. What it fetches stays in your
WingDrive library on this computer.
