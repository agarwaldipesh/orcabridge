#!/usr/bin/env python3
"""Add the five hooks to ~/.claude/settings.json, pointing at this folder.

PreCompact:   save the latest messages and last reply word for word before any compaction.
SessionStart: name the notes file; after a compaction or /clear, paste the notes and those words back once.
PostToolUse:  from 200K, and at every further 100K, remind the session to write notes and compact|clear.
SessionEnd:   on /clear, save the old session's words for the new session in the same terminal.
Stop:         when the session chose compact|clear (or at the 500K backstop), schedule it through Orca.

Backs the settings file up first and keeps its file mode. Safe to run twice (adds nothing the second
time). Does not set autoCompactWindow (see README). `--dry-run` only prints what it would add.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def wanted():
    """(event, command, timeout) for each hook."""
    python = shlex.quote(sys.executable)
    notes = '%s %s ' % (python, shlex.quote(os.path.join(HERE, 'session_notes.py')))
    return [
        ('PreCompact', notes + 'precompact', 10),
        ('SessionStart', notes + 'sessionstart', 10),
        ('PostToolUse', notes + 'remind', 5),
        ('SessionEnd', notes + 'sessionend', 10),
        ('Stop', '%s %s' % (python, shlex.quote(os.path.join(HERE, 'self_compact_hook.py'))), 5),
    ]


def install(path, dry_run=False):
    """Return the events added (or that would be added)."""
    try:
        with open(path) as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    hooks = settings.setdefault('hooks', {})
    added = []
    for event, command, timeout in wanted():
        entries = hooks.setdefault(event, [])
        if any(command == h.get('command') for e in entries for h in e.get('hooks', [])):
            continue
        entries.append({'hooks': [{'type': 'command', 'command': command, 'timeout': timeout}]})
        added.append((event, command))
    if added and not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(settings, fh, indent=2)
        if os.path.exists(path):
            shutil.copy2(path, path + '.bak-%s-orcabridge' % time.strftime('%Y%m%d%H%M%S'))
            shutil.copymode(path, tmp)  # keep the settings file private (often 0600)
        else:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dry-run', action='store_true', help='print what would be added; change nothing')
    parser.add_argument(
        '--settings',
        default=os.path.expanduser('~/.claude/settings.json'),
        help='settings file (default: ~/.claude/settings.json)',
    )
    args = parser.parse_args(argv)
    added = install(args.settings, args.dry_run)
    verb = 'would add' if args.dry_run else 'added'
    for event, command in added:
        print('%s %s: %s' % (verb, event, command))
    if not added:
        print('nothing to add (already there)')


if __name__ == '__main__':
    main()
