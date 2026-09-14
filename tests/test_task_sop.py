import stat
import subprocess
import tempfile
from pathlib import Path
import unittest

from src.agent.task_sop import DEPLOYMENT_SOP


class DeploymentSopTests(unittest.TestCase):
    def test_template_repairs_and_preserves_unrelated_content(self):
        command = 'set -e\n' + DEPLOYMENT_SOP.split('set -e\n', 1)[1].split('\n在上述命令', 1)[0]
        for original in ('keep\n', 'one\r\ntwo\r\nold\r\nfour\r\nfive\r\nold\r\nlast'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'config').mkdir()
                config = root / 'config/beta.conf'
                config.write_bytes(original.encode())
                (root / 'bin').mkdir()
                script = root / 'bin/start.sh'
                script.write_text('#!/bin/sh\necho keep\n')
                script.chmod(0o600)
                for _ in range(2):
                    result = subprocess.run(['sh', '-c', command], cwd=root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                lines = config.read_text().splitlines()
                self.assertEqual(lines[2], '替换为本题第3行完整内容')
                self.assertEqual(lines[5], '替换为本题第6行完整内容')
                self.assertEqual(lines[0], original.splitlines()[0])
                if '\r\n' in original:
                    self.assertEqual(config.read_bytes().count(b'\r\n'), 6)
                    self.assertEqual(lines[-1], 'last')
                self.assertEqual(script.read_text(), '#!/bin/sh\necho keep\n')
                for path in (script, root / 'logs/beta'):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_failed_precondition_stops_final_checker(self):
        command = 'set -e\n' + DEPLOYMENT_SOP.split('set -e\n', 1)[1].split('\n在上述命令', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['sh', '-c', command + '\ntouch checker-ran'], cwd=tmp,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(tmp) / 'checker-ran').exists())
