# orcabridge

[![CI](https://github.com/agarwaldipesh/orcabridge/actions/workflows/ci.yml/badge.svg)](https://github.com/agarwaldipesh/orcabridge/actions/workflows/ci.yml)

Keep a long Claude Code session's work when its context is compacted or cleared, and (in Orca)
let the session compact itself at a natural break instead of in the middle of a task.

## The problem

A long Claude Code session fills its context window. When it compacts, the automatic summary
often drops what matters most: the exact ask, promises made, running jobs, the file and line you
were on, ideas already ruled out. Compaction also tends to happen mid-task. And after `/clear`,
the new session knows nothing about the old one.

## How it works

1. **Notes file.** Each session gets a notes file, `<notes dir>/<session id>.md`. At session start
   it is told the path and the sections to fill (purpose, goal, done, open problems, the user's
   open asks, running jobs, exact files and commands, approaches ruled out, next step). It
   overwrites it at the end of each task.
2. **Reminders.** After every tool call (PostToolUse), the context size is read from the
   transcript. At 200K tokens, and again at every further 100K, the session is asked to refresh
   its notes at its next natural break and then write one word into its ready file:
   `compact` (same task continues) or `clear` (task done, next is unrelated). From 500K the ask
   says to refresh the notes now, even mid-task. Reminders only fire inside an Orca terminal,
   because only there can anything act on the ready file.
3. **Stop hook.** When the session finishes a turn, `self_compact_hook.py` checks for a fresh
   ready file. If there is none it does nothing, unless the session has passed 500K (the
   backstop for sessions that never pause). It skips when a helper agent is still running (unless
   the session chose `compact`), when a compaction already happened in the last 30 minutes, or
   one is already pending. Before a backstop compaction it asks the session once to update its
   notes.
4. **Quiet wait and safety checks.** The hook starts `orca_compact.py --wait` in the background.
   The waiter waits for this turn's end in Orca's status file, then (if the user typed the last
   prompt) three minutes of quiet so the user can read the reply. It cancels on any new event,
   an approval prompt, a busy session, or a draft in the input box. To tell Claude Code's greyed
   suggestion from a real draft it types one probe character, checks the box reads exactly that,
   erases it, and only then sends.
5. **Compaction.** It sends `/compact <note>`, where the note tells the summary to preserve
   everything in the notes file, or `/clear`.
6. **PreCompact** saves the last three messages from the user and the session's last reply, word
   for word, next to the notes.
7. **SessionStart** after a compaction pastes the notes and those saved words back into the
   session, once.
8. **/clear handover.** On `/clear`, SessionEnd records which session this Orca terminal held;
   the new session in the same terminal gets the old session's notes and last words (within 15
   minutes, once).
9. **Last net.** Claude Code's own `autoCompactWindow` still compacts sessions that never stop;
   the notes are pasted back afterwards just the same.

Parts 1, 6-7 and 9 (notes, saved words, paste-back, the last net) work in plain Claude Code.
The reminders, the self-compaction and the `/clear` handover need Orca and its `orca` CLI
(by Stably AI), which can read and type into the session's terminal.

## Install

Requires Python 3.9+ (standard library only) and Claude Code.

```sh
pipx install git+https://github.com/agarwaldipesh/orcabridge.git
orcabridge-install --dry-run   # shows the five hooks it would add
orcabridge-install             # adds them; backs up settings.json first
```

`pipx` keeps the tool in its own environment (on macOS: `brew install pipx`). Inside a virtual
environment, `pip install` with the same URL works too.

Or from a clone, without pip:

```sh
git clone https://github.com/agarwaldipesh/orcabridge.git
cd orcabridge
python3 -m orcabridge.install --dry-run
python3 -m orcabridge.install
```

The hooks point at the installed (or cloned) `orcabridge/` files and run with the same Python
the installer ran with. Running it twice adds nothing. `--settings <path>` writes to another
settings file instead of `~/.claude/settings.json`.

Then set Claude Code's own automatic compaction a little above the 500K backstop, in
`~/.claude/settings.json`:

```json
"autoCompactWindow": 580000
```

Why: the self-compaction acts only when a turn ends. A session fed by a stream of helper results
may never end a turn; this setting makes Claude Code compact it before the context is full,
and the notes it was told to keep fresh from 500K are pasted back. The installer does not change
this setting for you.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_NOTES_DIR` | `~/.claude/session-notes` | notes, saved words, ready files, per-terminal records |
| `SESSION_NOTES_STATE_DIR` | `<notes dir>/state` | waiter markers, fingerprint files, `self-compact.log` |
| `ORCA_BIN` | `/Applications/Orca.app/Contents/Resources/bin/orca` | the orca CLI |
| `ORCA_STATUS_PATH` | `~/Library/Application Support/Orca/agent-hooks/last-status.json` | Orca's hook status file |

Orca sets `ORCA_TERMINAL_HANDLE` and `ORCA_PANE_KEY` in its terminals; outside Orca they are
missing and the Orca-only parts stay off. Thresholds (200K, +100K, 500K, quiet 180 s) are
constants in `session_notes.py`, `self_compact_hook.py` and `orca_compact.py`.

## Notes template

The session is asked to write, under 80 lines, overwriting each time:

Purpose of the next stretch of work; Goal; Approach; Done; Failing or open problems; the user's
open asks and what you promised; Running jobs and deadlines; Exact files and line numbers,
commands in progress; Approaches ruled out and why; Next step; Suggested skills. Point to
tickets, plans, commits and diffs instead of copying them. Never write secrets.

## Prompts sent by another session

If another tool (e.g. a coordinator session) types prompts into a terminal, nobody needs the
three-minute quiet wait to read the reply. Such a tool can record what it typed in
`<state dir>/bridge-sent-<terminal handle>.json`:

```json
{"sha": ["<sha256 hex of the exact prompt text, UTF-8>", "..."]}
```

Keep the last ten or so. When every prompt in the current turn matches a fingerprint, the quiet
wait is skipped. If the file is missing, the wait always happens.

## Files

All code is in the `orcabridge/` package:

- `session_notes.py` - notes, reminders, save before compaction, paste-back (PreCompact,
  SessionStart, PostToolUse, SessionEnd).
- `self_compact_hook.py` - the Stop hook that decides whether to compact or clear.
- `orca_compact.py` - the background waiter that types the command through Orca.
- `install.py` - adds the hooks to `~/.claude/settings.json` (the `orcabridge-install` command).

Tests are in `tests/`: run `python3 -m unittest discover tests` from the repository root
(everything mocked; nothing is typed anywhere). See CONTRIBUTING.md for lint.

## Limits

- Self-compaction relies on reading Orca's screen as plain text and its status file; a change in
  Claude Code's prompt layout or Orca's status format can make it cancel (it fails safe: nothing
  is sent).
- Context size is read from the transcript's last usage record, so it lags one reply.
- The notes are only as good as what the session writes. Nothing is added on ordinary turns.
- Saved words are cut at 3000 characters per message; notes are pasted back up to 8000.
- Hooks always exit 0 and never block a tool call; an error simply means no reminder or no
  compaction this time (the waiter logs one line per decision in `self-compact.log`, never
  transcript content).
- macOS paths are the defaults; elsewhere set the variables above.
