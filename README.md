# cmux-ws-manager

List [cmux](https://cmux.com) workspaces you've closed, and reopen them.

cmux has a native History menu and `cmux restore-session` for its previous saved
session, but `cmux list-workspaces` only shows open workspaces. This tool reads
cmux's closed-item history to provide a searchable picker and numbered CLI
reopen commands for individual closed workspaces.

Run it with no arguments and you get an interactive picker:

```
  Closed workspaces                                                       3
──────────────────────────────────────────────────────────────────────────
▸   1  Aug 24 20:58   API rewrite             ~/code/api-server
                                              feature/auth  2T  1AI  1web
    2  Aug 24 20:56   docs-site               ~/code/docs-site
                                              1T
    3  Aug 19 01:24   code                    ~/code
                                              3T  1AI*
 ↑↓ move  ⏎ reopen  / search  r refresh  ? keys  q / esc quit
```

Arrow to a workspace, press enter, and it comes back. Each entry's second line
shows the git branch and what a reopen would restore: terminals (`T`),
resumable AI sessions (`AI`), and browser tabs (`web`). Entries cmux has no
snapshot for say `no snapshot` — those can only be reopened empty.

`AI*` means the session was carried over from an earlier close of the same
workspace — see [Sessions that outlive their snapshot](#sessions-that-outlive-their-snapshot).
`dir open` means another workspace is already sitting on that directory, so
reopening this one puts a second workspace there.
`remote snapshot` means the workspace needs cmux's native History menu to
restore its SSH/cloud connection. This tool refuses layout restoration for
those entries; the command-line `reopen <n> --plain` opens only a local shell.

### Keys

| key | action |
| --- | --- |
| `↑` `↓` / `k` `j` | move |
| `PgUp` `PgDn` / `Home` `End` / `g` `G` | page, first, last |
| `enter` | reopen with the saved layout, then exit |
| `/` | filter by title or path; `enter` keeps it, `esc` clears it |
| `r` | reload |
| `?` | key help |
| `q` / `esc` | quit without doing anything; `esc` clears an active filter first |

## Non-interactive use

For scripts and agents, `--no-interactive` prints a plain numbered listing and
exits:

```
$ cmux-ws-manager --no-interactive
  1  Aug 24 20:58  API rewrite                  ~/code/api-server  [feature/auth]
  2  Aug 24 20:56  docs-site                    ~/code/docs-site
  3  Aug 19 01:24  code                         ~/code

Reopen one with: cmux-ws-manager reopen <n>

$ cmux-ws-manager reopen 2
```

The same listing is printed automatically — no flag needed — whenever stdin or
stdout is not a terminal, when `TERM` is unset or `dumb`, or if curses cannot
start. Piping the bare command is therefore always safe: it never draws escape
codes into the pipe and never waits for a keypress. Every entry is exactly one
line, and `NO_COLOR` is honored in the picker.

`reopen <n>` is the scriptable equivalent of pressing enter.
The command-line option `reopen <n> --plain` opens an empty workspace at the
saved directory and title, without restoring the layout or sessions.

Run `cmux-ws-manager --help` for the full option list. An unrecognized option
is a usage error, not a silent no-op.

## What you see

The default view shows one row per reopenable workspace. A closed workspace is
hidden when an open one already stands in for it: same id, or the same
directory *and* the same custom title. Repeated closes of that same workspace
collapse to the newest.

Directory alone is not enough to identify a workspace, because you can keep
several on one repo — a "Group 1" and a "CMUX Stuff" both on `~/code/api`. If
closing either were hidden by the other still being open, it would be
unreachable, since hidden entries surface only under `--all`, where
`reopen <n>` numbering does not apply. A title you set yourself is what tells
them apart, and it survives the reopen; a closed workspace with no custom title
still collapses per directory, exactly as before.

The trade-off is renames. Reopen a workspace, rename it, and its old close
comes back as a row — its directory is occupied by a name that no longer
matches. Those rows carry a `dir open` badge, so it is visible that reopening
adds a second workspace on that path.

`cmux-ws-manager --all` shows the raw close history instead — in the picker it
replaces the row numbers with `·`, because `reopen <n>` numbering always
follows the default view.

`reopen <n>` rebuilds the workspace from cmux's snapshot: split layout,
terminals starting at their original directories, custom pane names, supported
AI sessions, and browser URLs. It converts the snapshot's layout tree into a
`cmux new-workspace --layout` call. Workspaces captured when an entire window
was closed are listed individually and reopen in the caller's window.

Supported AI providers are Claude Code, Codex, and OpenCode. With current cmux,
each fresh terminal first registers a manual `cmux surface resume set` binding
using the provider's standard resume command, then runs `cmux restore` for that
surface and checkpoint. A checkpoint id alone cannot restore into a fresh pane:
cmux resolves it against the calling surface's binding. Registering it also lets
the next close retain the session identity.

The binding uses `claude --resume <session>`, `codex resume <session>`, or
`opencode --session <session>`. Original custom launch arguments, environment
overrides, hook provenance, and approval settings are not copied from history.
cmux versions without the surface-binding API use the provider command directly.
When the binding API is available, a failed binding or restore leaves the shell
available without a second launch attempt. Restore runs in a subshell, so the
shell also survives when the agent exits.

### Sessions that outlive their snapshot

That is also why an entry can show `AI*`. If a workspace was reopened and closed
again by a version of this tool that typed the resume command, the newer close
recorded no session — and since repeated closes of one workspace collapse to the
newest, it hid the close that still had one. When the newest close of a workspace
has no AI session and an earlier close of that same workspace within the last
seven days does, that session is carried onto the newer snapshot and the count is
marked with `*`. Donors are matched by workspace, never merely by directory, so a
session is never carried over from a neighbour sharing the path.

Nothing identifying survives a reopen (cmux regenerates panel ids and
`stableSurfaceId`), so the pane is matched by its title and working directory,
then by position, and only ever a pane that has no session of its own. A
mismatch resumes the conversation one pane over, at the right directory.
`--all` never grafts: it is the raw close history.

Not restorable: scrollback, running non-agent processes, browser profiles and
back/forward history, canvas positions, workspace groups, or SSH/cloud
connections. Other panel types (including file previews and embedded agent chat)
become terminal placeholders. Agent resume requires the provider and its session
files to still exist. Missing or malformed layout snapshots reopen empty.
`reopen <n> --plain` opens an empty local workspace at the saved directory with
the original title. Creation has a 30-second timeout; if it times out, check
whether the workspace appeared before retrying.

## How it works

Two sources, merged and deduplicated:

1. **cmux's native closed-item history**
   (`~/Library/Application Support/cmux/closed-item-history-<bundle-id>.json`) —
   the app records closed workspaces, panels, and windows, with cwd,
   git branch, layout snapshot, the workspace's custom title, and — for a pane
   cmux has bound to an agent — a `resumeBinding` with the checkpoint id that
   `cmux restore` takes after a matching surface binding is installed. Primary
   source; nothing needs to run in the background. Retention is bounded by the
   app: cmux 0.64.24 defaults to 500 total records and at most 100 workspace
   records, rather than a fixed number of days.
2. **The cmux event log** (`~/.cmuxterm/events.jsonl`) — cmux appends every
   event here, but rotates it at 16 MiB with a single archive, so busy
   sessions age events out within a day. Each run of `cmux-ws-manager` harvests
   the workspace-category events into `~/.cmuxterm/workspace-history.jsonl`,
   which never rotates. These events supply titles for records that predate
   cmux storing them, closes older than the native retention, and the
   `workspace.created` events used to drop workspaces that were later
   reopened.

Open windows are read through `cmux list-windows --json`, followed by
`cmux workspace list --json` for each window, which supplies workspace titles.
A failed window query does not discard results from other windows. Closed
entries matching an open workspace's id — or its directory and title — are
filtered out (unless `--all`).

Titles and paths come from those files, so they are treated as untrusted:
control characters, tabs, newlines, and bidi overrides are neutralized before
anything is displayed or printed.
Malformed records are skipped, and unavailable history files do not prevent
listing the remaining sources. Both the current native history envelope and
legacy arrays of records are accepted.

## Compatibility and checks

Reviewed against cmux **0.64.24** (`f5da007dd`), including its changes since
August 26, 2026. Newer private snapshot fields are ignored unless supported.

The regression checks use Python's standard library, temporary home directories,
and a fake `cmux`; they do not modify your cmux history:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile cmux-ws-manager
git diff --check
```

## Install

```sh
ln -s "$PWD/cmux-ws-manager" ~/.local/bin/cmux-ws-manager   # or anywhere on PATH
```

macOS only — it reads cmux's own history out of
`~/Library/Application Support`. Requires Python 3 (stdlib only, including
`curses`) and the `cmux` CLI on PATH for the open-workspace filter and
`reopen`.

## License

LGPL-3.0-or-later — see [COPYING.LESSER](./COPYING.LESSER), which applies on
top of the GNU GPL v3 in [COPYING](./COPYING).
