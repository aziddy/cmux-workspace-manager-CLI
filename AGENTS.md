# AGENTS.md

## Scope

These instructions apply to the entire repository.

## Project overview

`cmux-ws-manager` is a macOS-only, single-file Python CLI that lists closed cmux
workspaces and reopens them. The executable is `./cmux-ws-manager`; it uses only
the Python standard library and the `cmux` CLI at runtime.

The tool combines two sources of workspace history:

- cmux's native closed-item history under
  `~/Library/Application Support/cmux/`
- workspace events in `~/.cmuxterm/events.jsonl*`, harvested into the
  non-rotating `~/.cmuxterm/workspace-history.jsonl`

It also queries currently open cmux windows/workspaces so the default list
does not offer already-open directories. Reopening from a native snapshot
reconstructs the pane layout, terminal directories, resumable AI sessions,
and browser URLs.

## Repository layout

- `cmux-ws-manager`: executable and all application logic
- `README.md`: user-facing behavior, installation, and limitations
- `COPYING` and `COPYING.LESSER`: LGPL-3.0-or-later license text
- `.gitignore`: local Python and macOS artifacts

Keep the project lightweight. Do not add a package manager, third-party
runtime dependency, or build system unless the task explicitly requires it.

## Development conventions

- Support the system `python3` on macOS and use standard-library APIs.
- Preserve the executable shebang and executable bit on `cmux-ws-manager`.
- Follow the existing style: four-space indentation, compact functions, and
  small data dictionaries passed between parsing/filtering/restoration steps.
- Pass subprocess arguments as lists. Do not build a shell command for
  `subprocess.run` or enable `shell=True`.
- Shell text embedded in a cmux layout is an exception because cmux types it
  into a live shell. Quote every user- or file-derived value with
  `shlex.quote` before adding it to that text.
- Give external cmux calls finite timeouts and continue to degrade gracefully
  when cmux, its history files, or malformed records are unavailable.
- Treat history inputs as untrusted and version-variable. Prefer tolerant
  `.get(...)` access and skip malformed records rather than failing the
  complete listing.
- Do not print raw event payloads, session files, or other private local data.
- Retain the LGPL-3.0-or-later header when moving or splitting source code.

## Behavioral invariants

Preserve these unless a requested change explicitly alters them:

- A normal invocation may append newly observed workspace events to
  `workspace-history.jsonl`; it must not modify cmux's native history or
  rotating event logs.
- Harvested events are deduplicated by event ID.
- Native history is preferred when both sources describe the same closed
  workspace because it includes the restorable snapshot.
- A later `workspace.created` event removes an earlier event-log-only close.
- The default view excludes workspaces that are open by ID, or whose
  normalized directory is open under the same custom title, and collapses
  repeated closes of one workspace to the newest. A closed record with no
  custom title is still excluded by directory alone and still collapses per
  directory. A shown entry whose directory is open elsewhere is marked.
- `--all` exposes raw close history, but `reopen <n>` numbering always refers
  to the default filtered list.
- Workspace titles fall back from known custom title to directory basename to
  `(untitled)`.
- `reopen <n> --plain` creates only an empty workspace at the saved directory.
- `-h`/`--help` prints usage to stdout and exits 0. It must short-circuit
  before any history read or `cmux` invocation, so it neither appends to
  `workspace-history.jsonl` nor requires cmux to be installed.
- An unrecognized option is a usage error (non-zero exit, message on stderr,
  `--help` hint), not a silently ignored argument.
- Snapshot restoration must leave a usable shell when an agent resume command
  is missing, fails, or later exits.

When changing CLI output or behavior, update `README.md` in the same change.

## Validation

There is currently no automated test suite. At minimum, run:

```sh
python3 -m py_compile cmux-ws-manager
```

For parsing or filtering changes, prefer fixture-based checks with a temporary
`HOME` and a fake `cmux` executable on `PATH`; do not overwrite real files in
`~/Library/Application Support/cmux` or `~/.cmuxterm`.

For restore changes, verify both `reopen <n>` and `reopen <n> --plain` on
macOS with cmux installed when that environment is available. Cover terminal,
browser, split-layout, Claude resume, Codex resume, missing snapshot, malformed
snapshot, and invalid index cases relevant to the change.

Before finishing, also review:

```sh
git diff --check
git diff
```
