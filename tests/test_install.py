import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orcabridge import install  # noqa: E402


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'settings.json')
        with open(self.path, 'w') as fh:
            json.dump(
                {'autoCompactWindow': 1, 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'other'}]}]}}, fh
            )
        os.chmod(self.path, 0o600)

    def read(self):
        with open(self.path) as fh:
            return fh.read()

    def test_dry_run_changes_nothing(self):
        before = self.read()
        added = install.install(self.path, dry_run=True)
        self.assertEqual([e for e, _ in added], ['PreCompact', 'SessionStart', 'PostToolUse', 'SessionEnd', 'Stop'])
        self.assertEqual(self.read(), before)
        self.assertEqual(os.listdir(self.tmp), ['settings.json'])

    def test_adds_once_backs_up_and_keeps_mode(self):
        self.assertEqual(len(install.install(self.path)), 5)
        self.assertEqual(install.install(self.path), [])
        settings = json.loads(self.read())
        self.assertEqual(settings['autoCompactWindow'], 1)  # left alone
        self.assertEqual(settings['hooks']['Stop'][0]['hooks'][0]['command'], 'other')  # kept
        self.assertIn('self_compact_hook.py', settings['hooks']['Stop'][1]['hooks'][0]['command'])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(len([n for n in os.listdir(self.tmp) if '.bak-' in n]), 1)


if __name__ == '__main__':
    unittest.main()
