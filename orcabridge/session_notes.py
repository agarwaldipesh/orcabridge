#!/usr/bin/env python3
"""Keep a session's work across compaction or /clear (Claude Code hooks).

  precompact    save the latest messages to the session and its last reply, word for word
  sessionend    on /clear: the same save, plus which session this Orca terminal held
  sessionstart  startup/resume: one line naming the session's notes file
                compact: paste the notes file and the saved words back, once
                clear: the same, from the session this terminal held before /clear
  remind        PostToolUse, in Orca: once at 200K and once more at every further 100K (300K, 400K,
                ...), ask the session to write its notes and a ready file (compact|clear) at its
                next natural break; the Stop hook (self_compact_hook.py) acts on the
                ready file. From URGENT_TOKENS the ask says to refresh the notes now, even mid-task.
                After a compaction the count starts again from 200K.

Each session writes its own notes file (the template below) at the end of each task and when
reminded. Nothing is added on ordinary turns. Always exits 0.
"""

import json
import os
import re
import shutil
import sys
import time

NOTES_DIR = os.environ.get('SESSION_NOTES_DIR') or os.path.expanduser('~/.claude/session-notes')
TAIL_BYTES = 4 * 1024 * 1024
KEEP_MESSAGES = 3
MAX_CHARS = 3000  # per saved message or reply
NOTES_MAX_CHARS = 8000
SAVED_MAX_CHARS = (KEEP_MESSAGES + 1) * (MAX_CHARS + 100) + 500  # everything precompact can write
SAVED_FRESH_SECONDS = 15 * 60  # older saved words belong to an earlier compaction
TEMPLATE = (
    "Purpose of the next stretch of work; Goal; Approach; Done; Failing or open problems; the user's open "
    "asks and what you promised them; Running jobs and deadlines; Exact files and line numbers, commands in "
    "progress; Approaches ruled out and why; Next step; Suggested skills. Point to tickets, plans, commits "
    "and diffs instead of copying them; never write secrets (keys, passwords, connection strings, tokens); "
    "under 80 lines; overwrite, don't append"
)
# Ask from 200K, then again at every further 100K.
REMIND_TOKENS = 200_000
REMIND_STEP_TOKENS = 100_000
# Sessions that never pause (helper results feed straight back in) never reach the Stop hook, so
# Claude Code's own auto-compact (autoCompactWindow 580000 in ~/.claude/settings.json, see README) catches them
# mid-task. From URGENT_TOKENS the ask says to keep the notes fresh for it.
URGENT_TOKENS = 500_000
_BAND_RE = re.compile(r'band (\d+)')
REMIND_TAIL_BYTES = 1024 * 1024  # runs after every tool call: read less
CLEAR_FRESH_SECONDS = 15 * 60  # a by-terminal record older than this belongs to an earlier /clear
_HANDLE_RE = re.compile(r'term_[A-Za-z0-9-]+')


def paths(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,64}', session_id):
        raise ValueError('bad session id')
    return os.path.join(NOTES_DIR, session_id + '.md'), os.path.join(NOTES_DIR, session_id + '.last.md')


def ready_path(session_id):
    """compact|clear, written by the session when reminded; the Stop hook overwrites it once used."""
    return paths(session_id)[0][:-3] + '.ready'


def _terminal_record(env):
    handle = env.get('ORCA_TERMINAL_HANDLE') or ''
    if not _HANDLE_RE.fullmatch(handle):
        return None
    return os.path.join(NOTES_DIR, 'by-terminal', handle + '.json')


def _write(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(tmp, path)


def last_usage_tokens(path, tail=TAIL_BYTES):
    """Context size of the last assistant message (input + cache read + cache creation), or None
    when none follows the last compaction (right after one, the old size is still the last on file)."""
    with open(path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - tail))
        lines = fh.read().splitlines()
    for line in reversed(lines):
        if b'"compact_boundary"' in line:
            return None
        if b'"usage"' not in line:
            continue
        try:
            usage = json.loads(line)['message']['usage']
        except (ValueError, KeyError, TypeError):
            continue
        return sum(
            int(usage.get(k) or 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
        )
    return None


def _text(content):
    if isinstance(content, list):
        content = '\n'.join(x.get('text', '') for x in content if isinstance(x, dict) and x.get('type') == 'text')
    return content.strip() if isinstance(content, str) else ''


def _clip(text):
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + ' [...cut]'


def last_words(transcript_path):
    """Latest prompts typed into the session (the user, or another session through Orca) and
    the session's last written reply; tool output is never copied."""
    with open(transcript_path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - TAIL_BYTES))
        lines = fh.read().splitlines()
    prompts, reply = [], None
    for line in reversed(lines):
        if len(prompts) >= KEEP_MESSAGES and reply is not None:
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get('message') if isinstance(entry.get('message'), dict) else {}
        if entry.get('type') == 'assistant' and reply is None:
            reply = _text(message.get('content')) or None
        elif (
            entry.get('type') == 'user'
            and isinstance(entry.get('origin'), dict)
            and entry['origin'].get('kind') == 'human'
            and len(prompts) < KEEP_MESSAGES
        ):
            text = _text(message.get('content'))
            if text:
                prompts.append((entry.get('timestamp', '')[:16].replace('T', ' '), text))
    out = ['## Latest messages to this session, oldest first (word for word)']
    out += ['- %s UTC: %s' % (at, _clip(text)) for at, text in reversed(prompts)] or ['- (none found)']
    out += ['', '## Your last reply before compaction (word for word)', _clip(reply or '(none found)')]
    return '\n'.join(out) + '\n'


def precompact(payload):
    _, last = paths(payload.get('session_id'))
    _write(last, last_words(payload['transcript_path']))


def sessionend(payload, env, now):
    """On /clear: save the old session's last words and remember it for this Orca terminal."""
    record = _terminal_record(env)
    if payload.get('reason') != 'clear' or record is None:
        return
    paths(payload.get('session_id'))
    _write(record, json.dumps({'session_id': payload['session_id'], 'at': now}))
    precompact(payload)


def remind(payload, env, now):
    """The reminder text when due, else None. Cheap checks first: this runs after every tool call."""
    if payload.get('agent_id') or _terminal_record(env) is None:
        return None  # a helper's tool call, or not in Orca (nothing would act on the ready file)
    sid = payload.get('session_id')
    notes, _ = paths(sid)
    mark = notes[:-3] + '.reminded'  # 'band N': the last 100K band this session was asked at
    tokens = last_usage_tokens(payload['transcript_path'], tail=REMIND_TAIL_BYTES)
    if tokens is None:
        return None  # right after a compaction there is no new size yet
    band = tokens // REMIND_STEP_TOKENS
    try:
        with open(mark, encoding='utf-8') as fh:
            found = _BAND_RE.fullmatch(fh.read().strip())
        asked = int(found.group(1)) if found else None  # an old time-stamp marker counts as none
    except OSError:
        asked = None
    if asked is not None and band < asked:
        _write(mark, 'band %d' % band)  # compacted since: count up again from here
        return None
    if tokens < REMIND_TOKENS or (asked is not None and band <= asked):
        return None
    _write(mark, 'band %d' % band)
    if tokens >= URGENT_TOKENS:
        return (
            'Context is at %dK. Claude Code will compact this session automatically before 580K, even mid-task, '
            'and pastes your session notes back afterwards. Overwrite your session notes %s now (sections: %s), '
            'including exactly what is in progress, then carry on. When you reach a natural break, also write '
            '`compact` or `clear` into %s with a Bash echo and finish your turn.'
            % (tokens // 1000, notes, TEMPLATE, ready_path(sid))
        )
    return (
        'Context is at %dK. At your next natural break (not in the middle of a task), overwrite your session '
        'notes %s (sections: %s), then write the single word `compact` (you keep working on the same task) or '
        '`clear` (your task is finished and what comes next is unrelated) into %s with a Bash echo, '
        'then finish your turn. If you are mid-task, carry on; you will be asked again at the next 100K.'
        % (tokens // 1000, notes, TEMPLATE, ready_path(sid))
    )


def _read(path, limit):
    try:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
    except OSError:
        return None
    return text if len(text) <= limit else text[:limit] + '\n[...cut]'


def _previous_session(env, now):
    """The session this Orca terminal held just before /clear (used once), or None."""
    record = _terminal_record(env)
    if record is None:
        return None
    try:
        with open(record, encoding='utf-8') as fh:
            doc = json.load(fh)
        old = doc['session_id']
        paths(old)
        if now - float(doc['at']) >= CLEAR_FRESH_SECONDS:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    _write(record, json.dumps({'consumed': old, 'at': now}))
    return old


def sessionstart(payload, env=None, now=None):
    env, now = os.environ if env is None else env, time.time() if now is None else now
    notes, last = paths(payload.get('session_id'))
    rule = (
        'Your session notes file is %s (sections: %s). Overwrite it at the end of each task and when '
        'reminded; it is pasted back to you after compaction or /clear. '
        'Do not re-read it otherwise.' % (notes, TEMPLATE)
    )
    source = payload.get('source')
    old = _previous_session(env, now) if source == 'clear' else None
    if source != 'compact' and old is None:
        return rule
    if old is not None:
        old_notes, last = paths(old)
        try:
            shutil.copyfile(old_notes, notes)
        except OSError:
            pass
        intro = 'This session was just cleared (/clear). What the previous session (%s) saved before it:' % old
    else:
        intro = 'This session was just compacted. What you saved before it:'
    parts = [intro, '', '# Your session notes (%s)' % notes, _read(notes, NOTES_MAX_CHARS) or '(no notes file yet)', '']
    try:
        fresh = now - os.path.getmtime(last) < SAVED_FRESH_SECONDS
    except OSError:
        fresh = False
    saved = _read(last, SAVED_MAX_CHARS) if fresh else None
    if saved:
        parts += [saved, '']
    parts.append(rule)
    return '\n'.join(parts)


def main():
    try:
        payload = json.loads(sys.stdin.read() or '{}')
        if sys.argv[1:] == ['precompact']:
            precompact(payload)
        elif sys.argv[1:] == ['sessionend']:
            sessionend(payload, os.environ, time.time())
        elif sys.argv[1:] == ['sessionstart']:
            print(
                json.dumps(
                    {
                        'hookSpecificOutput': {
                            'hookEventName': 'SessionStart',
                            'additionalContext': sessionstart(payload),
                        }
                    }
                )
            )
        elif sys.argv[1:] == ['remind']:
            text = remind(payload, os.environ, time.time())
            if text:
                print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': text}}))
    except Exception:
        pass  # never block a tool call, a compaction or a session start


if __name__ == '__main__':
    main()
    sys.exit(0)
