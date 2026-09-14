import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from src.agent import GameServer
from src.agent.protocol import GameState, Strategy, TaskSession, ActionValidator

EXPECTED = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


class P0Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.server.prepare_directories()
        self.client = self.server.app.test_client()

    def test_valid_fixture_and_log(self):
        payload = json.loads((Path(__file__).parent / "fixtures/valid_request.json").read_text(encoding="utf-8"))
        response = self.client.post("/", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, EXPECTED)
        self.assertEqual(json.loads((self.root / "logs/request_000001.json").read_text(encoding="utf-8")), payload)
        self.assertTrue((self.root / "state").is_dir())
        self.assertEqual(list((self.root / "state").iterdir()), [])

    def test_invalid_then_recovery(self):
        for payload in ['{broken', 'null', '[]', '1', '"text"', '']:
            with self.subTest(payload=payload):
                response = self.client.post("/", data=payload, content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json, {"error": "invalid JSON object"})
        self.assertEqual(list((self.root / "logs").iterdir()), [])
        self.assertEqual(self.client.post("/", json={}).json, EXPECTED)

    def test_restart_preserves_logs(self):
        self.client.post("/", json={"first": True})
        new_server = GameServer(self.root)
        other = new_server.app.test_client()
        other.post("/", json={"second": True})
        self.assertEqual(json.loads((self.root / "logs/request_000001.json").read_text()), {"first": True})
        self.assertEqual(json.loads((self.root / "logs/request_000002.json").read_text()), {"second": True})

    def test_only_post_root(self):
        self.assertEqual(self.client.get("/").status_code, 405)
        self.assertEqual(self.client.post("/other", json={}).status_code, 404)
        self.assertEqual(self.client.post("/", data='{}', content_type="text/plain").status_code, 400)

    def test_normal_flow(self):
        response = self.client.post("/", json={"opaque": 1})
        self.assertEqual(response.status_code, 200)
        result = response.json
        self.assertIn("roleCommandMap", result)
        self.assertIn("prompt", result)
        self.assertIn("executeCmd", result)

    def test_recovery_after_error(self):
        response1 = self.client.post("/", json={})
        self.assertEqual(response1.status_code, 200)
        response2 = self.client.post("/", json={})
        self.assertEqual(response2.status_code, 200)

    def test_interfaces_are_unimplemented(self):
        for interface in [GameState, Strategy, TaskSession, ActionValidator]:
            with self.assertRaises(TypeError):
                interface()

    def test_real_http_process(self):
        # 按上传目录布局验证真实入口，日志写入临时目录。
        project = Path(__file__).parent.parent
        shutil.copy2(project / "main3.py", self.root / "main3.py")
        shutil.copy2(project / "run.sh", self.root / "run.sh")
        shutil.copytree(project / "src", self.root / "src")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # Start the service via the main module
        process = subprocess.Popen(
            [sys.executable, "main3.py", str(port)] if os.name == 'nt' else ["bash", "run.sh", str(port)],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        try:
            for _ in range(100):
                self.assertIsNone(process.poll(), "service exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            for payload, status in [(b'{}', 200), (b'{broken', 400), (b'{}', 200)]:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("POST", "/", payload, {"Content-Type": "application/json"})
                response = conn.getresponse()
                body = json.loads(response.read())
                conn.close()
                self.assertEqual(response.status, status)
                if status == 200:
                    self.assertEqual(body, EXPECTED)
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            output, _ = process.communicate(timeout=5)
            print("\nReal HTTP server output:\n" + output.decode("utf-8", errors="replace"))
        self.assertEqual(len(list((self.root / "logs").glob("request_*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
