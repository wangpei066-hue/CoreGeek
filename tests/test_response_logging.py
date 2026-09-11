import json
from pathlib import Path
import tempfile
import unittest

from src.agent import GameServer


class ResponseLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.server.prepare_directories()
        self.client = self.server.app.test_client()

    def test_response_is_logged_with_matching_sequence(self):
        response = self.client.post("/", json={"roundNo": 1})
        self.assertEqual(response.status_code, 200)
        request_path = self.root / "logs/request_000001.json"
        response_path = self.root / "logs/response_000001.json"
        self.assertTrue(request_path.is_file())
        self.assertTrue(response_path.is_file())
        self.assertEqual(json.loads(response_path.read_text(encoding="utf-8")), response.json)

    def test_invalid_request_does_not_create_response_log(self):
        self.client.post("/", data="{broken", content_type="application/json")
        self.assertEqual(list((self.root / "logs").glob("response_*.json")), [])

    def test_sequence_numbers_stay_paired_across_multiple_rounds(self):
        self.client.post("/", json={"roundNo": 1})
        self.client.post("/", json={"roundNo": 2})
        for seq in ("000001", "000002"):
            self.assertTrue((self.root / f"logs/request_{seq}.json").is_file())
            self.assertTrue((self.root / f"logs/response_{seq}.json").is_file())


if __name__ == "__main__":
    unittest.main()
