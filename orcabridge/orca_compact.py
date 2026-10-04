#!/usr/bin/env python3
"""Orca waiter for automatic compaction: `python3 orca_compact.py --wait` reads a job (JSON) on stdin.

Started detached by self_compact_hook.py. Waits for the session's turn to end and the terminal to be
idle and quiet, then types `/compact <note>` (or `/clear`) into the session's Orca terminal through
the orca CLI, or cancels. Logs one outcome line per job.
"""

import json
import os
import re
import subprocess
import sys
import time

ORCA = os.environ.get('ORCA_BIN') or '/Applications/Orca.app/Contents/Resources/bin/orca'
STATUS_PATH = os.environ.get('ORCA_STATUS_PATH') or os.path.expanduser(
    '~/Library/Application Support/Orca/agent-hooks/last-status.json'
)
NOTES_DIR = os.environ.get('SESSION_NOTES_DIR') or os.path.expanduser('~/.claude/session-notes')
# Markers, the decision log and the fingerprint files live here.
STATE_DIR = os.environ.get('SESSION_NOTES_STATE_DIR') or os.path.join(NOTES_DIR, 'state')
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
UUID_PATTERN = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}'
PANE_RE = re.compile(r'^%s:%s$' % (UUID_PATTERN, UUID_PATTERN))


# Backup only: these common credential shapes are redacted; arbitrary secrets may remain.
_REDACTED = '[REDACTED]'
_CREDENTIAL_FIELD_NAMES = (
    r'(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'auth(?:orization)?|token|cookie|set-cookie|client[_-]?secret|'
    r'private[_-]?key|secret)'
)
_CREDENTIAL_KEY_RE = re.compile(rf'^{_CREDENTIAL_FIELD_NAMES}$', re.I)
_PRIVATE_KEY_RE = re.compile(r'-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----', re.I | re.S)
_CREDENTIAL_URL_RE = re.compile(
    r'(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/\s:@]+(?::[^/@\s]*)?)@(?P<host>[^/\s]+)', re.I
)
_QUOTED_CREDENTIAL_RE = re.compile(
    rf'(?P<label>(?<![\w-])(?:\\?["\'])?{_CREDENTIAL_FIELD_NAMES}'
    rf'(?:\\?["\'])?\s*(?::|=)\s*)(?P<quote>\\?["\'])'
    rf'(?P<value>(?:\\.|(?!(?P=quote)).)*?)(?P=quote)',
    re.I | re.S,
)
_HEADER_CREDENTIAL_RE = re.compile(
    r'(?P<label>(?<![\w-])(?:\\?["\'])?(?:authorization|cookie|set-cookie)'
    r'(?:\\?["\'])?\s*(?::|=)\s*)(?P<value>(?!\\?["\'\[])'
    r'[^\r\n,;}]+)',
    re.I,
)
_UNQUOTED_CREDENTIAL_RE = re.compile(
    rf'(?P<label>(?<![\w-])(?:\\?["\'])?{_CREDENTIAL_FIELD_NAMES}'
    rf'(?:\\?["\'])?\s*(?::|=)\s*)(?P<value>(?!\\?["\'\[])'
    r'[^\s,;}\]]+)',
    re.I,
)


def _redact_text(value):
    value = _PRIVATE_KEY_RE.sub('[REDACTED PRIVATE KEY]', value)
    value = _CREDENTIAL_URL_RE.sub(lambda match: match.group('scheme') + _REDACTED + '@' + match.group('host'), value)
    value = _QUOTED_CREDENTIAL_RE.sub(
        lambda match: match.group('label') + match.group('quote') + _REDACTED + match.group('quote'), value
    )
    value = _HEADER_CREDENTIAL_RE.sub(lambda match: match.group('label') + _REDACTED, value)
    return _UNQUOTED_CREDENTIAL_RE.sub(lambda match: match.group('label') + _REDACTED, value)


def cli(*args):
    result = subprocess.run([ORCA, 'terminal', *args, '--json'], capture_output=True, text=True, timeout=55)
    if result.returncode:
        raise ValueError('Orca command failed: ' + result.stderr[-1000:])
    response = json.loads(result.stdout)
    if not response.get('ok'):
        raise ValueError(str(response.get('error', 'Orca rejected the request')))
    return response['result']


def handle(value):
    if not isinstance(value, str) or not re.fullmatch(r'term_[a-zA-Z0-9-]+', value):
        raise ValueError('An exact terminal handle from `orca terminal list` is required')
    return value


def screen(target):
    return cli('read', '--terminal', target, '--screen')['terminal']


def identity(target):
    matches = [item for item in cli('list', '--limit', '100')['terminals'] if item['handle'] == target]
    if len(matches) != 1:
        raise ValueError('Terminal no longer available')
    item = matches[0]
    if not item.get('connected') or not item.get('writable') or item.get('orphaned'):
        raise ValueError('Terminal is not connected and writable')
    return (item.get('incarnationId'), item.get('agentIdentity'))


def _pane(value):
    if not isinstance(value, str) or not PANE_RE.fullmatch(value):
        raise ValueError('pane_key must contain the exact tab and leaf UUIDs')
    return value.lower()


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            value = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError('Unreadable state: %s' % error) from error
    if not isinstance(value, dict):
        raise ValueError('State must be a JSON object')
    return value


def _write_json(path, value, exclusive=False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = '%s.%s.tmp' % (path, os.getpid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, indent=1, sort_keys=True)
            fh.write('\n')
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


SELF_COMPACT_LOG = os.path.join(STATE_DIR, 'self-compact.log')
SELF_COMPACT_WAIT_SECONDS = 600
# Auto mode (Stop hook, see self_compact_hook.py): the hook runs AT Stop, in parallel with Orca's own
# Stop hook, so Orca may record this Stop before or after the waiter's first status read.
SELF_COMPACT_AUTO_STOP_SLACK_MS = 5000  # a Stop received this close to the hook start is this Stop
SELF_COMPACT_AUTO_STOP_WAIT_SECONDS = 15  # Orca records Stop within ms; never wait for a second Stop
SELF_COMPACT_PENDING_SECONDS = 900  # a pending marker older than this is stale
# Auto mode waits this long untouched after the turn, so the user can read the reply before the
# 'Compacted' notice pushes it off screen; any new event in that time cancels (the next Stop retries).
SELF_COMPACT_AUTO_QUIET_SECONDS = 180
SELF_COMPACT_HELPERS_SETTLE_SECONDS = 5  # helpers running, turn not the user's: let the Stop bookkeeping land


def bridge_sent_path(target):
    """Fingerprints of the last instructions another tool typed into this terminal (optional file,
    format in the README). The Stop hook skips the quiet wait when the turn was started by one of
    them: another session (e.g. a coordinator), not the user, sent it, so nobody needs time to read
    the reply."""
    return os.path.join(STATE_DIR, 'bridge-sent-%s.json' % handle(target))


def _self_compact_marker(pane_key):
    """Per-pane marker: {"pending": true} while an auto waiter runs; rewritten (never removed)
    as {"pending": false} when it ends."""
    return os.path.join(STATE_DIR, 'self-compact-pending-%s.json' % _pane(pane_key).replace(':', '_'))


def _self_compact_auto_job(job):
    """Complete an auto (Stop hook) job with identity and status checks:
    own Claude terminal, exact pane in Orca status, no approval, prompt or helpers, idle input."""
    target, pane_key = handle(job['target']), _pane(job['pane_key'])
    incarnation, agent = identity(target)
    if agent != 'claude':
        raise ValueError('Self-compact supports only a Claude Code terminal; no input sent')
    entry = ((_read_json(STATUS_PATH) or {}).get('entries') or {}).get(pane_key)
    if not isinstance(entry, dict) or str(entry.get('paneKey', '')).lower() != pane_key:
        raise ValueError('This pane is not in current Orca status; no input sent')
    payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
    if (
        payload.get('state') == 'waiting'
        or payload.get('interactivePrompt')
        or (payload.get('subagents') and not job.get('helpers'))
    ):
        raise ValueError('Pane is waiting or has running helpers; no input sent')
    received = entry.get('receivedAt')
    if type(received) not in (int, float):
        raise ValueError('unreadable status')
    # Only refuse an approval here. The prompt often shows Claude Code's greyed suggestion right at
    # Stop, and the probe before sending handles a suggestion or a draft.
    view = screen(target)
    if view.get('source') != 'screen':
        raise ValueError('No reliable screen')
    if re.search(
        r'allow once|allow always|trust this|do you want to|\[y/n\]|\(y/n\)', '\n'.join(view.get('tail', [])[-8:]), re.I
    ):
        raise ValueError('Possible approval on screen')
    return dict(job, target=target, pane_key=pane_key, received=received, incarnation=incarnation)


def _self_compact_log(job, outcome):
    try:
        os.makedirs(os.path.dirname(SELF_COMPACT_LOG), mode=0o700, exist_ok=True)
        with open(SELF_COMPACT_LOG, 'a', encoding='utf-8') as fh:
            fh.write(
                json.dumps(
                    {
                        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                        'terminal': job.get('target'),
                        'outcome': outcome,
                        **({'mode': 'auto'} if job.get('auto_since') is not None else {}),
                        **({'action': job['mode']} if job.get('mode') else {}),
                    }
                )
                + '\n'
            )
    except OSError:
        pass


def _prompt_window(view):
    """8 screen lines from 3 above Claude Code's input line (the '❯' line right under the box's
    top rule), as the plain last-8-lines window holds on a normal screen. Running helpers are
    listed below the box, one line each, and push the input line out of that plain window.
    Falls back to the last 8 lines when no boxed prompt is found."""
    tail = view.get('tail', [])[-24:]
    for i in range(len(tail) - 1, 0, -1):
        if re.match(r'\s*[>❯]', tail[i]) and tail[i - 1].strip().startswith('─'):
            return tail[max(0, i - 3) : i + 5]
    return tail[-8:]


def _prompt_text(view):
    """Text on the input prompt line of a screen read ('' when empty, None when no prompt).

    When the session shows the terminal's own cursor, Orca moves typed text out of the screen
    into a separate 'draft' field and leaves a bare prompt glyph; that text counts too."""
    for line in reversed(_prompt_window(view)):
        match = re.match(r'\s*[>❯]\s*(.*)$', line)
        if match:
            draft = view.get('draft')
            draft = draft.strip() if isinstance(draft, str) else ''
            return ' '.join(part for part in (match.group(1).strip(), draft) if part)
    return None


def _self_compact_screen(view):
    """Idle-screen check after the turn: no approval wording, not busy, a prompt line present.
    Returns the prompt text, which may be Claude Code's greyed next-prompt suggestion."""
    tail = '\n'.join(_prompt_window(view))
    if view.get('source') != 'screen':
        raise ValueError('No reliable screen')
    if re.search(r'allow once|allow always|trust this|do you want to|\[y/n\]|\(y/n\)', tail, re.I):
        raise ValueError('Possible approval on screen')
    if re.search(r'esc to interrupt', tail, re.I):
        raise ValueError('session still busy')
    text = _prompt_text(view)
    if text is None:
        raise ValueError('No input prompt on screen')
    return text


def _self_compact_wait(job):
    """Send `/compact <note>` (or `/clear` when job['mode'] is 'clear') once the session's turn has
    ended and the terminal has stayed idle and quiet, or cancel.

    The job comes from the Stop hook, which runs AT Stop: job['auto_since'] is the hook start (ms).
    Cancels if anything other than this Stop happens first, if after it anything new happens, or
    if the session is not idle. The prompt is then checked by _self_compact_probe_send, which
    tells Claude Code's greyed next-prompt suggestion apart from a draft the user typed.
    """
    target, pane_key, note = handle(job['target']), job['pane_key'], job['note']
    mode = job.get('mode', 'compact')
    if mode not in ('compact', 'clear'):
        return 'cancelled: unknown mode'
    command = '/clear' if mode == 'clear' else '/compact ' + note
    after, incarnation = job['received'], job['incarnation']
    if job.get('helpers'):
        return _self_compact_helpers_wait(job, target, pane_key, command)
    auto = job['auto_since']
    deadline = time.monotonic() + SELF_COMPACT_WAIT_SECONDS
    stop_deadline = time.monotonic() + SELF_COMPACT_AUTO_STOP_WAIT_SECONDS
    stop_at = stop_seen = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        doc = _read_json(STATUS_PATH)
        entries = (doc or {}).get('entries')
        entry = entries.get(pane_key) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return 'cancelled: pane left Orca status'
        payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
        event, received = entry.get('hookEventName'), entry.get('receivedAt')
        if type(received) not in (int, float) or received < after:
            return 'cancelled: unreadable status'
        if stop_at is None:
            if event == 'Stop' and received >= auto - SELF_COMPACT_AUTO_STOP_SLACK_MS:
                # this turn's Stop, recorded before or after the job was built
                stop_at, stop_seen = received, time.monotonic()
                time.sleep(0.5)
                continue
            if received == after:
                if time.monotonic() >= stop_deadline:
                    return 'cancelled: Orca never recorded this Stop'
                continue
            if event != 'Stop':
                return 'cancelled: %s before the turn ended' % event
            stop_at, stop_seen = received, time.monotonic()
            time.sleep(0.5)  # let a user keystroke or follow-up event land before judging idleness
            continue
        if received != stop_at:
            return 'cancelled: %s after the turn ended' % event
        # Orca's 'monitoring' state also counts as idle: the hook refused only for running helpers,
        # so 'monitoring' here means Monitor watchers or background commands, which run on through
        # a compaction without a turn.
        idle = payload.get('state') == 'done' or (
            payload.get('state') == 'working' and payload.get('workingMode') == 'monitoring'
        )
        if not idle or payload.get('subagents') or payload.get('interactivePrompt'):
            return 'cancelled: session not idle after the turn'
        if job.get('quiet', True) and time.monotonic() - stop_seen < SELF_COMPACT_AUTO_QUIET_SECONDS:
            continue
        if identity(target) != (incarnation, 'claude'):
            return 'cancelled: terminal changed'
        return _self_compact_probe_send(target, pane_key, stop_at, command)
    return 'cancelled: turn did not end within %d s' % SELF_COMPACT_WAIT_SECONDS


def _self_compact_new_turn(path, offset):
    """True once the session's transcript shows a new turn after `offset`: a turn always starts
    with a prompt or queued-notification line. Replies don't count: the turn's own last reply is
    sometimes written just after the Stop hook read the size. Helpers write their own files."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(offset)
            lines = fh.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return True
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a line still being written; the next read sees it whole
        kind = record.get('type') if isinstance(record, dict) else None
        if kind == 'user' or (kind == 'queue-operation' and record.get('operation') == 'enqueue'):
            return True
    return False


def _self_compact_helpers_wait(job, target, pane_key, command):
    """A session that chose compact while its helpers run (the hook sets job['helpers']). Orca
    keeps showing 'working' and the helpers' events, so idleness comes from the session's own
    transcript instead; their results still arrive after the compaction."""
    helpers = job['helpers']
    idle = lambda: not _self_compact_new_turn(helpers['transcript'], helpers['offset'])
    quiet = SELF_COMPACT_AUTO_QUIET_SECONDS if job.get('quiet', True) else SELF_COMPACT_HELPERS_SETTLE_SECONDS
    start = time.monotonic()
    while time.monotonic() - start < quiet:
        time.sleep(0.5)
        entry = ((_read_json(STATUS_PATH) or {}).get('entries') or {}).get(pane_key)
        if not isinstance(entry, dict):
            return 'cancelled: pane left Orca status'
        payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
        if payload.get('state') == 'waiting' or payload.get('interactivePrompt'):
            return 'cancelled: approval waiting'
        if not idle():
            return 'cancelled: session started a new turn'
    if identity(target) != (job['incarnation'], 'claude'):
        return 'cancelled: terminal changed'
    return _self_compact_probe_send(target, pane_key, None, command, idle)


def _self_compact_probe_send(target, pane_key, stop_at, command, idle=None):
    """Auto mode, after the quiet wait: Claude Code's greyed next-prompt suggestion is usually
    showing by now, and on the plain-text screen it reads like a draft. Probe instead: type one
    character. Typing replaces a suggestion, so the prompt reads exactly 'x' only when it held no
    draft; then erase it and send the command (/compact or /clear) in one input. Otherwise erase it
    (a draft is left as it was) and cancel."""
    try:
        first = _self_compact_screen(screen(target))
        time.sleep(1)
        if _self_compact_screen(screen(target)) != first:
            return 'cancelled: text in the prompt is changing'
    except ValueError as error:
        return 'cancelled: ' + str(error)
    cli('send', '--terminal', target, '--text', 'x')
    probe = None
    try:
        time.sleep(0.5)
        probe = _prompt_text(screen(target))
        if idle is None:
            latest = ((_read_json(STATUS_PATH) or {}).get('entries') or {}).get(pane_key) or {}
            idle = lambda: latest.get('receivedAt') == stop_at
        if probe == 'x' and not idle():
            probe = 'new event'
    except ValueError as error:
        probe = str(error)
    finally:
        if probe != 'x':
            cli('send', '--terminal', target, '--text', '\x7f')
    if probe == 'new event':
        return 'cancelled: new event just before sending'
    if probe != 'x':
        return 'cancelled: text in the prompt'
    # Two sends: with --enter Orca pastes the text, so a backspace inside it would be typed as a
    # character (seen live: 'x/compact ...' left unsent in the prompt). A bare send types it.
    cli('send', '--terminal', target, '--text', '\x7f')
    time.sleep(0.3)
    # Emptied, Claude Code may show the same greyed suggestion again; typing replaced it, so it is
    # not a draft. Anything else (the 'x' left, new text) cancels.
    if _prompt_text(screen(target)) not in ('', first):
        return 'cancelled: probe character not erased'
    cli('send', '--terminal', target, '--text', command, '--enter')
    return 'sent'


def self_compact_wait_main():
    job = {}
    try:
        job = json.loads(sys.stdin.read())
        if job.get('auto_since') is None:
            raise ValueError('job has no auto_since (only Stop hook jobs are supported)')
        job = _self_compact_auto_job(job)
        outcome = _self_compact_wait(job)
    except Exception as error:
        outcome = 'cancelled: ' + _redact_text(str(error))[:300]
    finally:
        if isinstance(job, dict) and job.get('auto_since') is not None:
            try:
                _write_json(_self_compact_marker(job.get('pane_key')), {'pending': False})
            except (OSError, ValueError):
                pass
    _self_compact_log(job, outcome)


if __name__ == '__main__':
    if sys.argv[1:] == ['--wait']:
        self_compact_wait_main()
    else:
        print('usage: orca_compact.py --wait  (job JSON on stdin)', file=sys.stderr)
        sys.exit(2)
