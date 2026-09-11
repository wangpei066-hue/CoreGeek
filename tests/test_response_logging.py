import logging
import unittest
from pathlib import Path
import tempfile

from src.agent import GameServer


class ResponseLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.server.prepare_directories()
        self.client = self.server.app.test_client()

    def test_round_decision_is_logged_to_logger(self):
        with self.assertLogs("src.agent.server", level="INFO") as captured:
            response = self.client.post("/", json={"roundNo": 7})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("round 7 ->" in line for line in captured.output))

    def test_invalid_request_still_returns_400(self):
        response = self.client.post("/", data="{broken", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_decision_failure_returns_empty_command_map(self):
        # 缺少必要字段时策略可能空指令，但仍应 200（Demo：失败不拖垮服务）
        response = self.client.post("/", json={"roundNo": 1})
        self.assertEqual(response.status_code, 200)
        self.assertIn("roleCommandMap", response.json)


if __name__ == "__main__":
    unittest.main()
