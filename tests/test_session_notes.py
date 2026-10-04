import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orcabridge import session_notes  # noqa: E402

OLD, NEW = '0123abcd-0001', '0123abcd-0002'
ENV = {'ORCA_TERMINAL_HANDLE': 'term_abc-1'}
NOW = time.time()  # the saved words are judged fresh against their real mtime


class SessionNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, 'notes')
        p = patch.object(session_notes, 'NOTES_DIR', self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.transcript = os.path.join(self.tmp, 't.jsonl')

    def transcript_with(self, tokens):
        lines = [
            {
                'type': 'user',
                'origin': {'kind': 'human'},
                'timestamp': '2027-01-15T08:00:00Z',
                'message': {'content': 'please fix the report'},
            },
            {
                'type': 'assistant',
                'message': {
                    'content': [{'type': 'text', 'text': 'Fixed it.'}],
                    'usage': {
                        'input_tokens': 10,
                        'cache_read_input_tokens': tokens - 110,
                        'cache_creation_input_tokens': 100,
                    },
                },
            },
        ]
        with open(self.transcript, 'w') as fh:
            fh.writelines(json.dumps(line) + '\n' for line in lines)

    def payload(self, sid=OLD, **extra):
        return dict({'session_id': sid, 'transcript_path': self.transcript}, **extra)

    def test_remind_due_not_due_and_marker(self):
        self.transcript_with(199_999)
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW))
        self.transcript_with(212_000)
        self.assertIsNone(session_notes.remind(self.payload(), {}, NOW))  # not in Orca
        self.assertIsNone(session_notes.remind(self.payload(agent_id='a1'), ENV, NOW))  # a helper's tool call
        text = session_notes.remind(self.payload(), ENV, NOW)
        self.assertIn('Context is at 212K', text)
        self.assertIn(os.path.join(self.dir, OLD + '.md'), text)
        self.assertIn(os.path.join(self.dir, OLD + '.ready'), text)
        self.assertIn('Suggested skills', text)
        self.assertIn('next 100K', text)
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW + 3 * 3600))  # same 100K band: never again
        self.transcript_with(299_000)
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW))
        self.transcript_with(301_000)
        self.assertIn('Context is at 301K', session_notes.remind(self.payload(), ENV, NOW))  # next band: once
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW))

    def test_count_starts_again_after_a_compaction(self):
        self.transcript_with(410_000)
        self.assertIsNotNone(session_notes.remind(self.payload(), ENV, NOW))
        self.transcript_with(90_000)  # compacted
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW))
        self.transcript_with(205_000)
        self.assertIn('Context is at 205K', session_notes.remind(self.payload(), ENV, NOW))

    def test_old_time_stamp_marker_counts_as_not_asked(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, OLD + '.reminded'), 'w') as fh:
            fh.write(str(NOW))
        self.transcript_with(350_000)
        self.assertIn('Context is at 350K', session_notes.remind(self.payload(), ENV, NOW))

    def test_no_size_reading_right_after_a_compaction(self):
        self.transcript_with(570_000)
        with open(self.transcript, 'a') as fh:
            fh.write(json.dumps({'type': 'system', 'subtype': 'compact_boundary'}) + '\n')
            fh.write(json.dumps({'type': 'user', 'message': {'content': 'quotes "compact_boundary" in text'}}) + '\n')
        self.assertIsNone(session_notes.last_usage_tokens(self.transcript))  # old 570K is before the boundary
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW))
        with open(self.transcript, 'a') as fh:
            fh.write(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 76_000}}}) + '\n')
        self.assertEqual(session_notes.last_usage_tokens(self.transcript), 76_000)

    def test_urgent_wording_from_500k(self):
        self.transcript_with(499_000)
        self.assertIn('natural break (not in the middle', session_notes.remind(self.payload(), ENV, NOW))
        self.transcript_with(505_000)
        text = session_notes.remind(self.payload(), ENV, NOW)
        self.assertIn('Context is at 505K', text)
        self.assertIn('automatically before 580K', text)
        self.assertIn(os.path.join(self.dir, OLD + '.md'), text)
        self.assertIsNone(session_notes.remind(self.payload(), ENV, NOW + 3600))  # once per band

    def test_clear_hands_notes_to_the_new_session_once(self):
        self.transcript_with(350_000)
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, OLD + '.md'), 'w') as fh:
            fh.write('## Goal\nship the fix\n')
        session_notes.sessionend(self.payload(reason='logout'), ENV, NOW)  # only /clear hands over
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'by-terminal')))
        session_notes.sessionend(self.payload(reason='clear'), ENV, NOW)
        text = session_notes.sessionstart({'session_id': NEW, 'source': 'clear'}, ENV, NOW + 5)
        self.assertIn('just cleared', text)
        self.assertIn('ship the fix', text)
        self.assertIn('please fix the report', text)
        self.assertIn('Fixed it.', text)
        self.assertIn(os.path.join(self.dir, NEW + '.md'), text)
        with open(os.path.join(self.dir, NEW + '.md')) as fh:
            self.assertEqual(fh.read(), '## Goal\nship the fix\n')
        # once: a second start in the same terminal gets only the rule line
        again = session_notes.sessionstart({'session_id': NEW, 'source': 'clear'}, ENV, NOW + 6)
        self.assertTrue(again.startswith('Your session notes file is'))
        # a stale record (over 15 min) is ignored; no record or not in Orca never fails
        session_notes.sessionend(self.payload(reason='clear'), ENV, NOW)
        stale = session_notes.sessionstart({'session_id': NEW, 'source': 'clear'}, ENV, NOW + 16 * 60)
        self.assertTrue(stale.startswith('Your session notes file is'))
        self.assertTrue(
            session_notes.sessionstart({'session_id': NEW, 'source': 'clear'}, {}, NOW).startswith(
                'Your session notes file is'
            )
        )

    def test_compact_path_unchanged(self):
        self.transcript_with(350_000)
        session_notes.precompact(self.payload())
        os.utime(os.path.join(self.dir, OLD + '.last.md'), (NOW, NOW))
        text = session_notes.sessionstart({'session_id': OLD, 'source': 'compact'}, ENV, NOW + 5)
        self.assertTrue(text.startswith('This session was just compacted.'))
        self.assertIn('(no notes file yet)', text)
        self.assertIn('please fix the report', text)
        self.assertTrue(
            session_notes.sessionstart({'session_id': OLD, 'source': 'startup'}, ENV, NOW).startswith(
                'Your session notes file is'
            )
        )


if __name__ == '__main__':
    unittest.main()
