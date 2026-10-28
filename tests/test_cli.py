"""Unit tests for output modes and repeated SSH targets; no Docker required."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'compose-image-check.py'
spec = importlib.util.spec_from_file_location('checker', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class CLIBehavior(unittest.TestCase):
    def setUp(self):
        checker.results.clear()
        checker.metrics.clear()
        checker.VERBOSE = False
        checker.row('PULL', 'example/app', 'alpine:latest', 'registry differs', 'docker compose pull app')
        checker.row('OK', 'example/db', 'postgres:17', 'aligned')

    def test_default_report_is_only_table(self):
        output = StringIO()
        with redirect_stdout(output):
            checker.print_report(False)
        result = output.getvalue()
        self.assertIn('PULL', result)
        self.assertNotIn('PROGRESS', result)
        self.assertNotIn('SUMMARY', result)
        self.assertNotIn('NOTE:', result)
        self.assertNotIn('REGISTRY:', result)
        self.assertNotIn('example/db', result)

    def test_verbose_report(self):
        checker.VERBOSE = True
        output = StringIO()
        with redirect_stdout(output):
            checker.print_report(True)
        result = output.getvalue()
        self.assertIn('SUMMARY:', result)
        self.assertIn('NOTE:', result)
        self.assertIn('REGISTRY:', result)
        self.assertIn('example/db', result)

    def test_details_are_grouped_at_end_after_verbose_report(self):
        checker.VERBOSE = True
        output = StringIO()
        with redirect_stdout(output):
            checker.print_report(False, details=True)
        result = output.getvalue()
        table_end = result.index('registry differs')
        summary = result.index('SUMMARY:')
        actions = result.index('SUGGESTED COMMANDS (review before running):')
        command = result.index('docker compose pull app')
        self.assertLess(table_end, summary)
        self.assertLess(summary, actions)
        self.assertLess(actions, command)
        self.assertEqual(result.count('docker compose pull app'), 1)
        self.assertTrue(result.rstrip().endswith('docker compose pull app'))

    def test_multiple_ssh_and_failure_does_not_stop_next(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_ssh = Path(temp) / 'ssh'
            fake_ssh.write_text('#!/bin/sh\n' +
                'cat >/dev/null\n' +
                'case " $* " in\n' +
                '  *" root@bad "*) echo "remote unavailable" >&2; exit 9;;\n' +
                'esac\n' +
                'echo "STATUS test-result"\n', encoding='utf-8')
            fake_ssh.chmod(0o755)
            env = os.environ.copy()
            env['PATH'] = temp + os.pathsep + env.get('PATH', '')
            proc = subprocess.run([sys.executable, str(SCRIPT), '--ssh', 'root@good', '--ssh', 'root@bad', '--ssh', 'root@last'],
                                  capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('=== root@good ===', proc.stdout)
            self.assertIn('=== root@bad ===', proc.stdout)
            self.assertIn('=== root@last ===', proc.stdout)
            self.assertEqual(proc.stdout.count('STATUS test-result'), 2)
            self.assertIn('root@bad', proc.stderr)


if __name__ == '__main__':
    unittest.main()
