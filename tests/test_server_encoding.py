import os
import subprocess
import sys
import unittest
from pathlib import Path


class ServerEncoding(unittest.TestCase):
    def test_help_uses_unicode_output_under_cp949_console(self):
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'cp949'
        result = subprocess.run(
            [sys.executable, '-m', 'verinote', '--help'],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        help_text = result.stdout.decode('utf-8')
        self.assertIn('Verinote — versioned evidence wiki', help_text)
