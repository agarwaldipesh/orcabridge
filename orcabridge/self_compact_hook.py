#!/usr/bin/env python3
"""Claude Code Stop hook: schedule an automatic `/compact` or `/clear` at a natural break.

The session decides: reminded at 200K and every further 100K (session_notes.py remind), it writes its notes and then
compact|clear into its ready file at a natural break. This hook acts on a fresh ready file, or on
its own only at BACKSTOP_TOKENS (runaway sessions). Launches the detached auto waiter
(orca_compact.py --wait) only when the session runs in Orca, has no helper still running,
has no pending compact, and (backstop only) was not compacted in the last 30 min. Always exits 0 quickly and
prints nothing, except the one-time request to update the notes before compacting. Logs one
decision line (never transcript content) to the self-compact log.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

if __package__:
    from . import orca_compact, session_notes
else:  # run as a script by path, as Claude Code does
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import orca_compact
    import session_notes

BACKSTOP_TOKENS = 500_000
READY_FRESH_SECONDS = 2 * 60 * 60  # an older ready file was left over, not a decision for now
# Only running helpers block compaction. Background commands (deploy watches, timers, dev
# servers) run for hours and keep running through a compaction; their completion notice still
# arrives afterwards and the compaction note says to keep running jobs.
# A helper launched longer ago than this no longer blocks: session restarts kill helpers without
# a completion notice.
STALE_TASK_SECONDS = 2 * 60 * 60
RECENT_COMPACT_SECONDS = 30 * 60
# A prompt the user typed this long ago still earns the quiet wait, even if a later turn was started
# by a helper or another session: the user may still be reading that reply.
USER_READ_SECONDS = 5 * 60
# Before compacting, ask the session once to write its notes file (session_notes.py
# pastes it back after compaction), unless it already did in this long.
NOTES_FRESH_SECONDS = 15 * 60
TAIL_BYTES = 4 * 1024 * 1024
# Typed after /compact: the summary must carry the session notes the session just wrote, not only
# the notes file pasted back afterwards.
NOTE = (
    "Preserve in full everything in this session's latest notes (%s/<session id>.md): "
    "purpose, goal, open tickets, the user's approvals and exact asks word for word, promises, running jobs "
    "and deadlines, exact files and commands in progress, approaches ruled out, next step."
    % session_notes.NOTES_DIR.replace(os.path.expanduser('~'), '~', 1)
)
_DONE_RE = re.compile(
    rb'<task-id>([^<]+)</task-id>(?:(?!<task-id>).){0,4000}?<status>(?:completed|failed|killed|stopped)</status>', re.S
)


_last_usage_tokens = session_notes.last_usage_tokens


def _ready(payload, now):
    """(mode, written at, path) from the session's fresh ready file, or None."""
    try:
        path = session_notes.ready_path(payload.get('session_id'))
        written = os.path.getmtime(path)
        with open(path, encoding='utf-8') as fh:
            mode = fh.read(100).strip().strip('`').lower()
    except (OSError, ValueError):
        return None
    if mode not in ('compact', 'clear') or now - written >= READY_FRESH_SECONDS:
        return None
    return mode, written, path


def _user_turn(path, target, now):
    """True (wait) when the user typed a prompt in this turn or in the last few minutes; False when
    every prompt in that span came from a helper, a background task, or another session that
    typed it (its fingerprint is in the optional fingerprint file, see README). True whenever unsure."""
    with open(path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - TAIL_BYTES))
        lines = fh.read().splitlines()
    try:
        sent = (orca_compact._read_json(orca_compact.bridge_sent_path(target)) or {}).get('sha') or []
    except ValueError:
        return True
    seen = in_turn = False
    for line in reversed(lines):
        if b'"origin"' not in line and b'end_turn' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('type') == 'assistant' and (entry.get('message') or {}).get('stop_reason') == 'end_turn':
            if seen:
                in_turn = False  # the previous turn ended here; older prompts count only if recent
            continue
        origin = entry.get('origin')
        if entry.get('type') != 'user' or not isinstance(origin, dict):
            continue
        if not seen:
            seen = in_turn = True
        try:
            age = now - datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00')).timestamp()
        except (KeyError, AttributeError, ValueError):
            age = 0
        if not in_turn and age > USER_READ_SECONDS:
            break
        if origin.get('kind') != 'human':
            continue
        content = (entry.get('message') or {}).get('content')
        if isinstance(content, list):
            content = ''.join(x.get('text', '') for x in content if isinstance(x, dict) and x.get('type') == 'text')
        if not isinstance(content, str) or hashlib.sha256(content.encode('utf-8')).hexdigest() not in sent:
            return True
    return not seen


def _notes_nudge(payload, pane, now):
    """The instruction asking the session to write its notes before compaction, or None when
    they are fresh, it was already asked recently, or this Stop is itself a hook continuation."""
    if payload.get('stop_hook_active'):
        return None
    try:
        notes, _ = session_notes.paths(payload.get('session_id'))
    except ValueError:
        return None
    try:
        if now - os.path.getmtime(notes) < NOTES_FRESH_SECONDS:
            return None
    except OSError:
        pass
    mark = os.path.join(orca_compact.STATE_DIR, 'self-compact-nudged-%s.json' % pane.replace(':', '_'))
    try:
        asked = float((orca_compact._read_json(mark) or {}).get('at') or 0)
    except (ValueError, TypeError):
        asked = 0
    if now - asked < NOTES_FRESH_SECONDS:
        return None  # asked once already: compact anyway rather than loop
    orca_compact._write_json(mark, {'at': now})
    return (
        'This session is about to be compacted automatically. Overwrite your session notes file %s '
        '(sections: %s) so your work survives compaction. Then stop, with no other '
        'reply.' % (notes, session_notes.TEMPLATE)
    )


def _scan(path, now):
    """(running helper ids, seconds since the last compact boundary or None)."""
    with open(path, 'rb') as fh:
        data = fh.read()
    launched, last_compact, pos = {}, None, 0
    for line in data.splitlines(keepends=True):
        if b'async_launched' in line or b'compact_boundary' in line:
            try:
                entry = json.loads(line)
            except ValueError:
                entry = {}
            result = entry.get('toolUseResult') if isinstance(entry.get('toolUseResult'), dict) else {}
            task = result.get('agentId') if result.get('status') == 'async_launched' else None
            if isinstance(task, str):
                try:
                    age = now - datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00')).timestamp()
                except (KeyError, AttributeError, ValueError):
                    age = 0
                if age < STALE_TASK_SECONDS:
                    launched[task] = pos
                else:
                    launched.pop(task, None)
            if entry.get('type') == 'system' and entry.get('subtype') == 'compact_boundary':
                try:
                    last_compact = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00')).timestamp()
                except (KeyError, AttributeError, ValueError):
                    pass
        pos += len(line)
    done = {}
    for match in _DONE_RE.finditer(data):
        done[match.group(1).decode('utf-8', 'replace')] = match.start()
    running = [t for t, at in launched.items() if done.get(t, -1) < at]
    return running, (None if last_compact is None else now - last_compact)


def decide(payload, env, now):
    """Return (reason to skip or None, job)."""
    target, pane = env.get('ORCA_TERMINAL_HANDLE'), env.get('ORCA_PANE_KEY')
    if not target or not pane:
        return 'not orca', None
    try:
        orca_compact.handle(target)
        pane = orca_compact._pane(pane)
    except ValueError:
        return 'not orca', None
    job = {'target': target, 'pane_key': pane, 'note': NOTE, 'auto_since': now * 1000}
    path = payload.get('transcript_path') if isinstance(payload, dict) else None
    if not isinstance(path, str) or not os.path.isfile(path):
        return 'no transcript', job
    tokens = _last_usage_tokens(path)
    if tokens is None:
        return 'no usage', job
    ready = _ready(payload, now)
    if ready is None and tokens < BACKSTOP_TOKENS:
        return 'below backstop (%d)' % tokens, job
    running, since_compact = _scan(path, now)
    if ready and since_compact is not None and ready[1] <= now - since_compact:
        ready = None  # written before the last compaction: already used up
        if tokens < BACKSTOP_TOKENS:
            return 'ready file older than the last compaction', job
    # a session that chose compact goes ahead: its helpers' results still arrive afterwards and its
    # notes list them. A clear (which drops that tracking) and the automatic backstop still wait.
    if running and not (ready and ready[0] == 'compact'):
        return 'helper running (%d)' % len(running), job
    if not ready and since_compact is not None and since_compact < RECENT_COMPACT_SECONDS:
        return 'compacted %d s ago' % since_compact, job
    marker = orca_compact._read_json(orca_compact._self_compact_marker(pane)) or {}
    if marker.get('pending') and now - float(marker.get('at') or 0) < orca_compact.SELF_COMPACT_PENDING_SECONDS:
        return 'compact already pending', job
    if not ready:
        nudge = _notes_nudge(payload, pane, now)
        if nudge:
            job['nudge'] = nudge
            return 'asked to update notes first', job
    job['mode'] = ready[0] if ready else 'compact'
    if ready:
        job['ready'] = ready[2]
    job['quiet'] = _user_turn(path, target, now)
    if running:
        # Orca keeps reporting the helpers' own events, so the waiter judges idleness from this
        # session's transcript instead: a new turn always writes to it, helpers write elsewhere.
        job['helpers'] = {'transcript': path, 'offset': os.path.getsize(path)}
    return None, job


def main():
    job = {'auto_since': 0}
    try:
        now = time.time()
        reason, job = decide(json.loads(sys.stdin.read() or '{}'), os.environ, now)
        job = job or {'auto_since': 0}
        if reason:
            if job.get('nudge'):
                print(json.dumps({'decision': 'block', 'reason': job['nudge']}))
            orca_compact._self_compact_log(job, 'hook skip: ' + reason)
            return
        job.pop('ready', None)
        orca_compact._write_json(orca_compact._self_compact_marker(job['pane_key']), {'pending': True, 'at': now})
        try:
            waiter = subprocess.Popen(
                [orca_compact.PYTHON, os.path.join(orca_compact.CODE_DIR, 'orca_compact.py'), '--wait'],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                text=True,
            )
            waiter.stdin.write(json.dumps(job))
            waiter.stdin.close()
        except Exception:
            orca_compact._write_json(orca_compact._self_compact_marker(job['pane_key']), {'pending': False})
            raise
        orca_compact._self_compact_log(job, 'hook scheduled')
    except Exception as error:
        orca_compact._self_compact_log(job, 'hook skip: error ' + type(error).__name__)


if __name__ == '__main__':
    main()
    sys.exit(0)
