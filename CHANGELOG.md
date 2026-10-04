# Changelog

## 0.1.0 - 2026-10-04

Initial release.

- A notes file per session, the last messages and reply saved word for word, and both pasted
  back after compaction or `/clear` (PreCompact, SessionStart, SessionEnd hooks).
- Context-size reminders from 200K and at every further 100K (PostToolUse hook, in Orca).
- Stop hook and background waiter that send `/compact` or `/clear` through Orca at a natural
  break, with a 500K backstop.
- `orcabridge-install` command that adds the hooks to Claude Code's settings.
