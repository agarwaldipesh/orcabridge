import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orcabridge import orca_compact, self_compact_hook, session_notes  # noqa: E402


class OrcaCompactTest(unittest.TestCase):
    def test_redacts_common_credentials_but_keeps_normal_text(self):
        text = '\n'.join(
            [
                'normal text retained',
                'password=PASSWORD_CANARY api_key: API_KEY_CANARY token=TOKEN_CANARY',
                'Authorization: Bearer AUTH_CANARY',
                'Cookie: session=COOKIE_CANARY',
                'https://user:URL_PASSWORD_CANARY@example.test/path',
                '-----BEGIN PRIVATE KEY-----\\nPRIVATE_CANARY\\n-----END PRIVATE KEY-----',
                r'inline {\"password\": \"INLINE_PASSWORD_CANARY\"\\n\"api_key\": \"INLINE_KEY_CANARY\"}',
            ]
        )
        redacted = orca_compact._redact_text(text)
        for canary in (
            'PASSWORD_CANARY',
            'API_KEY_CANARY',
            'TOKEN_CANARY',
            'AUTH_CANARY',
            'COOKIE_CANARY',
            'URL_PASSWORD_CANARY',
            'PRIVATE_CANARY',
            'INLINE_PASSWORD_CANARY',
            'INLINE_KEY_CANARY',
        ):
            self.assertNotIn(canary, redacted)
        self.assertIn('normal text retained', redacted)
        self.assertIn('[REDACTED PRIVATE KEY]', redacted)


PANE = '11111111-1111-4111-8111-111111111111:22222222-2222-4222-8222-222222222222'
ORCA_ENV = {'ORCA_TERMINAL_HANDLE': 'term_self', 'ORCA_PANE_KEY': PANE}


class SelfCompactHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transcript = os.path.join(self.tmp, 't.jsonl')
        self.lines = []
        self.launched = []
        test = self

        class FakeStdin:
            def write(self, text):
                test.launched[-1]['job'] = json.loads(text)

            def close(self):
                pass

        class FakePopen:
            def __init__(self, argv, **kwargs):
                test.launched.append({'argv': argv, 'kwargs': kwargs})
                self.stdin = FakeStdin()

        self.patches = [
            patch.object(orca_compact, 'STATE_DIR', self.tmp),
            patch.object(orca_compact, 'SELF_COMPACT_LOG', os.path.join(self.tmp, 'log')),
            patch.object(self_compact_hook.subprocess, 'Popen', FakePopen),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def add(self, **entry):
        self.lines.append(entry)

    def usage(self, tokens):
        self.add(
            type='assistant',
            message={
                'id': 'm%d' % len(self.lines),
                'usage': {
                    'input_tokens': 10,
                    'cache_read_input_tokens': tokens - 110,
                    'cache_creation_input_tokens': 100,
                    'output_tokens': 5,
                },
            },
        )

    def done(self, task):
        self.add(
            type='queue-operation',
            content='<task-id>%s</task-id>\n<tool-use-id>x</tool-use-id>\n'
            '<status>completed</status>\n<summary>ok</summary>' % task,
        )

    def run_hook(self, env=ORCA_ENV, now=1_800_000_000.0):
        with open(self.transcript, 'w') as fh:
            fh.writelines(json.dumps(line) + '\n' for line in self.lines)
        stdin = io.StringIO(json.dumps({'session_id': 's', 'transcript_path': self.transcript}))
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, 'stdin', stdin),
            patch.object(self_compact_hook.time, 'time', lambda: now),
        ):
            self_compact_hook.main()
        with open(os.path.join(self.tmp, 'log')) as fh:
            return json.loads(fh.readlines()[-1])['outcome']

    def test_below_threshold_and_not_orca_skip(self):
        self.usage(499_999)
        self.assertEqual(self.run_hook(), 'hook skip: below backstop (499999)')
        self.usage(300_000)  # between 300K and 500K only a ready file acts
        self.assertEqual(self.run_hook(), 'hook skip: below backstop (300000)')
        self.usage(600_000)
        self.assertEqual(self.run_hook(env={}), 'hook skip: not orca')
        self.assertEqual(self.launched, [])

    def test_running_helper_skips_but_commands_and_monitors_do_not(self):
        self.add(type='user', toolUseResult={'status': 'async_launched', 'agentId': 'agent1'})
        self.usage(600_000)
        self.assertEqual(self.run_hook(), 'hook skip: helper running (1)')
        self.done('agent1')
        # a finished helper launched again is running again
        self.add(type='user', toolUseResult={'status': 'async_launched', 'agentId': 'agent1'})
        self.usage(600_000)
        self.assertEqual(self.run_hook(), 'hook skip: helper running (1)')
        self.done('agent1')
        # a background command (deploy watch, timer, dev server) never blocks: it keeps running
        # through a compaction and its completion notice still arrives afterwards
        self.add(type='user', toolUseResult={'stdout': '', 'backgroundTaskId': 'bash1'})
        # nor does a permanent Monitor watcher; its events carry no status
        self.add(type='user', toolUseResult={'taskId': 'mon1', 'persistent': True})
        self.add(type='user', content='<task-id>mon1</task-id><summary>Monitor event</summary>')
        self.usage(600_000)
        self.assertEqual(self.run_hook(), 'hook scheduled')
        self.assertEqual(len(self.launched), 1)

    def test_stale_helper_does_not_block(self):
        now = 1_800_000_000.0
        stamp = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))
        # launched 3 h ago and never reported done (killed by a restart)
        self.add(
            type='user', timestamp=stamp(now - 3 * 3600), toolUseResult={'status': 'async_launched', 'agentId': 'ghost'}
        )
        self.usage(600_000)
        self.assertEqual(self.run_hook(now=now), 'hook scheduled')
        # launched 10 min ago still blocks
        self.add(
            type='user', timestamp=stamp(now - 600), toolUseResult={'status': 'async_launched', 'agentId': 'fresh'}
        )
        self.usage(600_000)
        self.assertEqual(self.run_hook(now=now), 'hook skip: helper running (1)')

    def test_recent_compact_and_pending_marker(self):
        now = 1_800_000_000.0
        stamp = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))
        self.add(type='system', subtype='compact_boundary', timestamp=stamp(now - 600))
        self.usage(600_000)
        self.assertEqual(self.run_hook(now=now), 'hook skip: compacted 600 s ago')
        self.lines[0]['timestamp'] = stamp(now - 1900)
        marker = orca_compact._self_compact_marker(PANE)
        orca_compact._write_json(marker, {'pending': True, 'at': now - 60})
        self.assertEqual(self.run_hook(now=now), 'hook skip: compact already pending')
        orca_compact._write_json(marker, {'pending': True, 'at': now - 901})  # stale: ignored
        self.assertEqual(self.run_hook(now=now), 'hook scheduled')
        self.assertEqual(orca_compact._read_json(marker), {'pending': True, 'at': now})

    def test_quiet_only_when_the_user_started_the_turn(self):
        def quiet(**prompt):
            self.lines = []
            self.add(type='user', message={'content': 'hello'}, **prompt)
            self.usage(550_000)
            orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
            self.assertEqual(self.run_hook(), 'hook scheduled')
            return self.launched[-1]['job']['quiet']

        self.assertTrue(quiet(origin={'kind': 'human'}))
        self.assertFalse(quiet(origin={'kind': 'peer'}))  # a helper's report
        self.assertFalse(quiet(origin={'kind': 'task-notification'}))
        # typed by another session: its fingerprint file, in the format the README documents
        orca_compact._write_json(
            orca_compact.bridge_sent_path('term_self'), {'sha': [hashlib.sha256(b'hello').hexdigest()]}
        )
        self.assertFalse(quiet(origin={'kind': 'human'}))
        self.assertTrue(quiet())  # no origin recorded: unsure, so wait
        # the user's prompt, then a helper's report during the same turn: still their turn
        self.lines = []
        self.add(
            type='user', message={'content': 'a question'}, origin={'kind': 'human'}, timestamp='2020-01-01T00:00:00Z'
        )
        self.add(type='user', message={'content': 'report'}, origin={'kind': 'peer'}, timestamp='2020-01-01T00:01:00Z')
        self.usage(550_000)
        orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
        self.assertEqual(self.run_hook(), 'hook scheduled')
        self.assertTrue(self.launched[-1]['job']['quiet'])
        # an older turn of theirs, long ago, then a helper-started turn: no wait
        self.lines.insert(1, {'type': 'assistant', 'message': {'stop_reason': 'end_turn'}})
        orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
        self.assertEqual(self.run_hook(), 'hook scheduled')
        self.assertFalse(self.launched[-1]['job']['quiet'])

    def test_notes_nudge_once_before_compacting(self):
        notes_dir = os.path.join(self.tmp, 'notes')
        with patch.object(session_notes, 'NOTES_DIR', notes_dir):
            self.usage(600_000)
            payload = {'session_id': '0123abcd-0000', 'transcript_path': self.transcript}

            def run(now=1_800_000_000.0, **extra):
                with open(self.transcript, 'w') as fh:
                    fh.writelines(json.dumps(line) + '\n' for line in self.lines)
                out = io.StringIO()
                with (
                    patch.dict(os.environ, ORCA_ENV, clear=True),
                    patch.object(sys, 'stdin', io.StringIO(json.dumps(dict(payload, **extra)))),
                    patch.object(sys, 'stdout', out),
                    patch.object(self_compact_hook.time, 'time', lambda: now),
                ):
                    self_compact_hook.main()
                with open(os.path.join(self.tmp, 'log')) as fh:
                    return json.loads(fh.readlines()[-1])['outcome'], out.getvalue()

            outcome, printed = run()
            self.assertEqual(outcome, 'hook skip: asked to update notes first')
            self.assertEqual(json.loads(printed)['decision'], 'block')
            self.assertIn(os.path.join(notes_dir, '0123abcd-0000.md'), json.loads(printed)['reason'])
            self.assertEqual(self.launched, [])
            # the continuation Stop never asks again; the session is compacted
            self.assertEqual(run(stop_hook_active=True), ('hook scheduled', ''))
            orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
            # asked a minute ago but ignored: compact anyway, never loop
            self.assertEqual(run(now=1_800_000_060.0), ('hook scheduled', ''))
            orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
            # fresh notes: no question at all
            os.makedirs(notes_dir, exist_ok=True)
            with open(os.path.join(notes_dir, '0123abcd-0000.md'), 'w'):
                pass
            orca_compact._write_json(
                os.path.join(self.tmp, 'self-compact-nudged-%s.json' % PANE.replace(':', '_')), {'at': 0}
            )
            self.assertEqual(run(now=time.time() + 60), ('hook scheduled', ''))
            # the same notes, stale 20 minutes later: asked again
            orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
            self.assertEqual(run(now=time.time() + 20 * 60)[0], 'hook skip: asked to update notes first')

    def test_backstop_at_500k(self):
        self.usage(500_000)
        self.assertEqual(self.run_hook(), 'hook scheduled')
        self.assertEqual(self.launched[-1]['job']['mode'], 'compact')

    def test_session_decides_with_ready_file(self):
        notes_dir = os.path.join(self.tmp, 'notes')
        sid = '0123abcd-0000'
        ready = os.path.join(notes_dir, sid + '.ready')
        now = 1_800_000_000.0
        stamp = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))

        def run():
            with open(self.transcript, 'w') as fh:
                fh.writelines(json.dumps(line) + '\n' for line in self.lines)
            out = io.StringIO()
            orca_compact._write_json(orca_compact._self_compact_marker(PANE), {'pending': False})
            with (
                patch.dict(os.environ, ORCA_ENV, clear=True),
                patch.object(
                    sys, 'stdin', io.StringIO(json.dumps({'session_id': sid, 'transcript_path': self.transcript}))
                ),
                patch.object(sys, 'stdout', out),
                patch.object(self_compact_hook.time, 'time', lambda: now),
            ):
                self_compact_hook.main()
            self.assertEqual(out.getvalue(), '')  # never a notes nudge: the session just wrote them
            with open(os.path.join(self.tmp, 'log')) as fh:
                return json.loads(fh.readlines()[-1])['outcome']

        def write(text, age=60):
            os.makedirs(notes_dir, exist_ok=True)
            with open(ready, 'w') as fh:
                fh.write(text)
            os.utime(ready, (now - age, now - age))

        with patch.object(session_notes, 'NOTES_DIR', notes_dir):
            self.usage(350_000)
            self.assertEqual(run(), 'hook skip: below backstop (350000)')
            for mode in ('clear', 'compact'):
                write(mode + '\n')
                self.assertEqual(run(), 'hook scheduled')
                self.assertEqual(self.launched[-1]['job']['mode'], mode)
                self.assertNotIn('ready', self.launched[-1]['job'])
                # the waiter ended without compacting (e.g. cancelled): the choice stands, the next Stop retries
                self.assertEqual(run(), 'hook scheduled')
            count = len(self.launched)
            write('clear', age=3 * 3600)  # stale
            self.assertEqual(run(), 'hook skip: below backstop (350000)')
            write('bogus')
            self.assertEqual(run(), 'hook skip: below backstop (350000)')
            # written before the last compaction: already used up
            self.lines.insert(0, {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': stamp(now - 600)})
            write('compact', age=700)
            self.assertEqual(run(), 'hook skip: ready file older than the last compaction')
            # written after it: acted on, even within 30 min of that compaction
            write('compact', age=300)
            self.assertEqual(run(), 'hook scheduled')
            count += 1
            # a running helper still blocks, and the ready file waits for a later Stop
            self.add(type='user', toolUseResult={'status': 'async_launched', 'agentId': 'agent1'})
            write('clear')
            self.assertEqual(run(), 'hook skip: helper running (1)')
            with open(ready) as fh:
                self.assertEqual(fh.read(), 'clear')
            self.assertEqual(len(self.launched), count)
            # but a session that chose compact goes ahead with the helper still running
            write('compact')
            self.assertEqual(run(), 'hook scheduled')
            self.assertEqual(self.launched[-1]['job']['mode'], 'compact')
            self.assertEqual(self.launched[-1]['job']['helpers']['transcript'], self.transcript)

    def test_job_built_and_launched_detached(self):
        self.usage(550_000)
        self.assertEqual(self.run_hook(), 'hook scheduled')
        (run,) = self.launched
        self.assertEqual(
            run['argv'], [orca_compact.PYTHON, os.path.join(orca_compact.CODE_DIR, 'orca_compact.py'), '--wait']
        )
        self.assertTrue(run['kwargs']['start_new_session'])
        self.assertEqual(
            run['job'],
            {
                'target': 'term_self',
                'pane_key': PANE,
                'note': self_compact_hook.NOTE,
                'auto_since': 1_800_000_000_000.0,
                'mode': 'compact',
                'quiet': True,
            },
        )
        with open(os.path.join(self.tmp, 'log')) as fh:
            self.assertEqual(json.loads(fh.readline())['mode'], 'auto')


class SelfCompactAutoWaiterTest(unittest.TestCase):
    @staticmethod
    def entry(event, received, **payload):
        return {
            'entries': {
                PANE: {
                    'paneKey': PANE,
                    'hookEventName': event,
                    'receivedAt': received,
                    'payload': {'state': 'working', **payload},
                }
            }
        }

    def wait(
        self, docs, auto_since, received, prompt='', suggestion=False, shown_from=10, quiet=True, returns=False, **extra
    ):
        import itertools

        reads = itertools.chain(docs, itertools.repeat(docs[-1]))
        clock, sent, box, submitted = [0.0], [], [prompt], []
        self.submitted = submitted

        def fake(*args):
            if args[0] == 'list':
                return {
                    'terminals': [
                        {
                            'handle': 'term_self',
                            'connected': True,
                            'writable': True,
                            'incarnationId': 'one',
                            'agentIdentity': 'claude',
                        }
                    ]
                }
            if args[0] == 'read':
                shown = (
                    box[0] if clock[0] >= shown_from or box[0] != prompt else ''
                )  # suggestion or draft appears after the turn
                return {'terminal': {'source': 'screen', 'tail': ['Done', ('❯\xa0' + shown).rstrip()]}}
            sent.append(args)
            text = args[4]  # the fake prompt: typing replaces a suggestion, \x7f erases, Enter submits
            box[0] = '' if suggestion and box[0] == prompt else box[0]
            for ch in text:  # with --enter Orca pastes: a backspace stays a literal character
                box[0] = box[0][:-1] if ch == '\x7f' and '--enter' not in args else box[0] + ch
            if '--enter' in args:
                submitted.append(box[0])
                box[0] = ''
            elif returns and box[0] == '':
                box[0] = prompt  # emptied: Claude Code shows the same suggestion again
            return {'ok': True}

        job = {
            'target': 'term_self',
            'pane_key': PANE,
            'note': self_compact_hook.NOTE,
            'auto_since': auto_since,
            'received': received,
            'incarnation': 'one',
            'quiet': quiet,
            **extra,
        }
        with (
            patch.object(orca_compact, 'cli', fake),
            patch.object(orca_compact, '_read_json', lambda path: next(reads)),
            patch.object(orca_compact.time, 'sleep', lambda s: clock.__setitem__(0, clock[0] + s)),
            patch.object(orca_compact.time, 'monotonic', lambda: clock[0]),
        ):
            return orca_compact._self_compact_wait(job), sent

    def test_auto_job_accepts_a_suggestion_but_not_an_approval(self):
        status = {
            'entries': {
                PANE: {'paneKey': PANE, 'hookEventName': 'Stop', 'receivedAt': 1400, 'payload': {'state': 'done'}}
            }
        }

        def build(tail):
            def fake(*args):
                if args[0] == 'list':
                    return {
                        'terminals': [
                            {
                                'handle': 'term_self',
                                'connected': True,
                                'writable': True,
                                'incarnationId': 'one',
                                'agentIdentity': 'claude',
                            }
                        ]
                    }
                return {'terminal': {'source': 'screen', 'tail': tail}}

            with patch.object(orca_compact, 'cli', fake), patch.object(orca_compact, '_read_json', lambda path: status):
                return orca_compact._self_compact_auto_job(
                    {'target': 'term_self', 'pane_key': PANE, 'note': 'n', 'auto_since': 1500}
                )

        # the greyed suggestion right at Stop no longer cancels (the probe handles it before sending)
        self.assertEqual(build(['Done', '❯\xa0run the tests?'])['received'], 1400)
        with self.assertRaisesRegex(ValueError, 'Possible approval'):
            build(['Do you want to allow this? [y/n]', '❯'])

    def test_stop_recorded_before_or_after_job_start(self):
        stop = self.entry('Stop', 1400, state='done')
        # Orca saved this Stop before the job read the status
        self.assertEqual(self.wait([stop], auto_since=1500, received=1400)[0], 'sent')
        # Orca saves the Stop after the job read the status
        outcome, sent = self.wait([self.entry('PostToolUse', 1100), stop], auto_since=1300, received=1100)
        self.assertEqual(outcome, 'sent')
        self.assertEqual(
            sent,
            [
                ('send', '--terminal', 'term_self', '--text', 'x'),
                ('send', '--terminal', 'term_self', '--text', '\x7f'),
                ('send', '--terminal', 'term_self', '--text', '/compact ' + self_compact_hook.NOTE, '--enter'),
            ],
        )
        # a Stop from long before this hook belongs to an earlier turn and never counts
        self.assertEqual(
            self.wait([stop], auto_since=90000, received=1400), ('cancelled: Orca never recorded this Stop', [])
        )

    def test_clear_mode_sends_exactly_clear(self):
        stop = self.entry('Stop', 1400, state='done')
        outcome, sent = self.wait(
            [stop], auto_since=1500, received=1400, prompt='run the tests?', suggestion=True, mode='clear'
        )
        self.assertEqual(outcome, 'sent')
        self.assertEqual(self.submitted, ['/clear'])
        self.assertEqual(sent[-1], ('send', '--terminal', 'term_self', '--text', '/clear', '--enter'))
        # a draft still cancels, as for /compact
        self.assertEqual(
            self.wait([stop], auto_since=1500, received=1400, prompt='half', mode='clear')[0],
            'cancelled: text in the prompt',
        )
        self.assertEqual(self.wait([stop], auto_since=1500, received=1400, mode='rm')[0], 'cancelled: unknown mode')

    def test_monitoring_state_is_idle_and_missing_stop_cancels(self):
        monitoring = self.entry('Stop', 1400, state='working', workingMode='monitoring')
        self.assertEqual(self.wait([monitoring], auto_since=1500, received=1400)[0], 'sent')
        self.assertEqual(
            self.wait([self.entry('Stop', 1400, state='working')], auto_since=1500, received=1400)[0],
            'cancelled: session not idle after the turn',
        )
        self.assertEqual(
            self.wait([self.entry('PostToolUse', 1100)], auto_since=1300, received=1100),
            ('cancelled: Orca never recorded this Stop', []),
        )

    def test_quiet_wait_then_probe_keeps_suggestion_and_drafts(self):
        stop = self.entry('Stop', 1400, state='done')
        # the suggestion already showing at Stop (seen live): still sent
        outcome, sent = self.wait(
            [stop], auto_since=1500, received=1400, prompt='run the tests?', suggestion=True, shown_from=0
        )
        self.assertEqual(outcome, 'sent')
        self.assertEqual(self.submitted, ['/compact ' + self_compact_hook.NOTE])
        # emptied again, the same suggestion comes back (seen live): still sent, exactly the command
        self.assertEqual(
            self.wait([stop], auto_since=1500, received=1400, prompt='run the tests?', suggestion=True, returns=True)[
                0
            ],
            'sent',
        )
        self.assertEqual(self.submitted, ['/compact ' + self_compact_hook.NOTE])
        # a greyed suggestion is typed over and replaced: sent
        self.assertEqual(
            self.wait([stop], auto_since=1500, received=1400, prompt='run the tests?', suggestion=True)[0], 'sent'
        )
        # what the session actually receives is exactly the command, with no stray probe letter
        self.assertEqual(self.submitted, ['/compact ' + self_compact_hook.NOTE])
        # the user's draft: the probe character is erased again and the draft is left as it was
        outcome, sent = self.wait([stop], auto_since=1500, received=1400, prompt='half a message')
        self.assertEqual(outcome, 'cancelled: text in the prompt')
        self.assertEqual([a[4] for a in sent], ['x', '\x7f'])
        # a turn another session started: no quiet wait, so a message seconds later can't block it
        reads = [stop] * 20 + [self.entry('UserPromptSubmit', 1500)]
        self.assertEqual(self.wait(reads, auto_since=1500, received=1400, quiet=False)[0], 'sent')
        # any event during the quiet wait (the user replies, a notification) cancels; the next Stop retries
        reads = [stop] * 100 + [self.entry('UserPromptSubmit', 1500)]
        self.assertEqual(
            self.wait(reads, auto_since=1500, received=1400), ('cancelled: UserPromptSubmit after the turn ended', [])
        )

    def test_prompt_found_above_a_list_of_running_helpers(self):
        rule = '─' * 40
        box = [
            'Reply text: do you want to go ahead?',
            'more reply',
            '✻ Waiting for 3 background agents',
            rule,
            '❯\xa0',
            rule,
            '  status line',
            '  ⏵⏵ auto mode on · ← 5 agents',
            '  ⏺ main',
            '  ◯ executor  one',
            '  ◯ executor  two',
            '  ◯ executor  three',
        ]
        view = {'source': 'screen', 'tail': box}
        self.assertEqual(
            orca_compact._self_compact_screen(view), ''
        )  # a plain last-8-line window would miss the prompt
        busy = dict(view, tail=box[:2] + ['✻ Working… (esc to interrupt)'] + box[3:])
        with self.assertRaisesRegex(ValueError, 'still busy'):
            orca_compact._self_compact_screen(busy)
        drafted = dict(view, tail=box[:4] + ['❯\xa0half a message'] + box[5:])
        self.assertEqual(orca_compact._prompt_text(drafted), 'half a message')

    def test_typed_text_reported_as_orca_draft(self):
        # With the terminal's own cursor shown, Orca leaves a bare prompt glyph and reports the
        # typed text separately as 'draft' (seen live with Claude Code 2.1.289).
        rule = '─' * 40
        view = {'source': 'screen', 'tail': ['Done', rule, '❯', rule, '  ⏸ manual mode on']}
        self.assertEqual(orca_compact._prompt_text(view), '')
        self.assertEqual(orca_compact._prompt_text(dict(view, draft='x')), 'x')
        self.assertEqual(orca_compact._prompt_text(dict(view, draft='abx')), 'abx')  # a draft: probe cancels
        self.assertEqual(orca_compact._prompt_text(dict(view, draft=None)), '')
        self.assertIsNone(orca_compact._prompt_text({'source': 'screen', 'tail': ['no prompt'], 'draft': 'x'}))

    def test_compact_with_helpers_running_judged_from_the_transcript(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 't.jsonl')
        with open(path, 'w') as fh:
            fh.write(json.dumps({'type': 'assistant'}) + '\n')
        helpers = {'transcript': path, 'offset': os.path.getsize(path)}
        # Stop bookkeeping lands after the hook; helpers keep firing events and Orca says 'working'
        with open(path, 'a') as fh:  # the turn's own last reply can land after the hook read the size (seen live)
            fh.write(json.dumps({'type': 'assistant'}) + '\n')
            fh.write(json.dumps({'type': 'system', 'subtype': 'turn_duration'}) + '\n')
        busy = [self.entry('SubagentStop', 1400 + i, subagents=[{'id': 'h'}]) for i in range(30)]
        outcome, sent = self.wait(busy, auto_since=1500, received=1400, quiet=False, helpers=helpers)
        self.assertEqual(outcome, 'sent')
        self.assertEqual(self.submitted, ['/compact ' + self_compact_hook.NOTE])

        # the auto job accepts the running helpers only in this mode
        def build(job):
            def fake(*args):
                if args[0] == 'list':
                    return {
                        'terminals': [
                            {
                                'handle': 'term_self',
                                'connected': True,
                                'writable': True,
                                'incarnationId': 'one',
                                'agentIdentity': 'claude',
                            }
                        ]
                    }
                return {'terminal': {'source': 'screen', 'tail': ['Done', '❯']}}

            with patch.object(orca_compact, 'cli', fake), patch.object(orca_compact, '_read_json', lambda p: busy[0]):
                return orca_compact._self_compact_auto_job(
                    dict(job, target='term_self', pane_key=PANE, note='n', auto_since=1500)
                )

        self.assertEqual(build({'helpers': helpers})['received'], 1400)
        with self.assertRaisesRegex(ValueError, 'running helpers'):
            build({})
        # an approval (a helper's included) cancels
        waiting = [self.entry('PermissionRequest', 1400, state='waiting', subagents=[{'id': 'h'}])]
        self.assertEqual(
            self.wait(waiting, auto_since=1500, received=1400, quiet=False, helpers=helpers),
            ('cancelled: approval waiting', []),
        )
        # a draft still cancels at the probe
        self.assertEqual(
            self.wait(busy, auto_since=1500, received=1400, quiet=False, helpers=helpers, prompt='half')[0],
            'cancelled: text in the prompt',
        )
        # a new turn in the session (a helper's result arriving, the user's message) cancels
        for line in ({'type': 'queue-operation', 'operation': 'enqueue'}, {'type': 'user'}):
            with open(path, 'w') as fh:
                fh.write(json.dumps({'type': 'assistant'}) + '\n')
            with open(path, 'a') as fh:
                fh.write(json.dumps(line) + '\n')
            self.assertEqual(
                self.wait(busy, auto_since=1500, received=1400, quiet=False, helpers=helpers),
                ('cancelled: session started a new turn', []),
            )
        # the user's own turn keeps the 3-minute quiet wait
        with open(path, 'w') as fh:
            fh.write(json.dumps({'type': 'assistant'}) + '\n')
        self.assertEqual(self.wait(busy, auto_since=1500, received=1400, helpers=helpers)[0], 'sent')
        self.assertGreaterEqual(orca_compact.SELF_COMPACT_AUTO_QUIET_SECONDS, 180)


if __name__ == '__main__':
    unittest.main()
