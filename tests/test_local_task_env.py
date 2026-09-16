import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.agent.local_task_env import LocalTaskEnvironment
from tools.extract_local_tasks import extract
from tools.run_local_task import DashScopeLLM, PLATFORM_OUTPUT_LIMIT, run_platform_command


class LocalTaskEnvironmentTests(unittest.TestCase):
    def test_platform_command_result_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            normal = run_platform_command("printf ok; exit 3", Path(directory))
            self.assertEqual(normal, "[exitCode:3]\nok")
            large = run_platform_command(
                "python3 -c \"import sys; sys.stdout.write('x'*70000)\"", Path(directory))
            self.assertTrue(large.startswith("[exitCode:0]\n"))
            self.assertTrue(large.endswith("\n[TRUNCATED]"))
            self.assertLessEqual(len(large.encode()), PLATFORM_OUTPUT_LIMIT + 40)

    def test_dashscope_stream_collects_only_final_content(self):
        class Delta:
            def __init__(self, content=None, reasoning_content=None):
                self.content, self.reasoning_content = content, reasoning_content

        class Chunk:
            def __init__(self, delta=None):
                self.choices = [] if delta is None else [type("Choice", (), {"delta": delta})()]

        client = DashScopeLLM.__new__(DashScopeLLM)
        client.model, client.enable_thinking = "qwen-test", False
        calls = []
        create = lambda **kwargs: (calls.append(kwargs) or iter([
            Chunk(Delta(reasoning_content="private thought")), Chunk(),
            Chunk(Delta(content='{"action":')), Chunk(Delta(content='"submit","taskAnswer":"42"}')),
        ]))
        client.client = type("Client", (), {"chat": type("Chat", (), {
            "completions": type("Completions", (), {"create": staticmethod(create)})()
        })()})()
        self.assertEqual(client("prompt"), '{"action":"submit","taskAnswer":"42"}')
        self.assertEqual(calls[0]["extra_body"], {"enable_thinking": False})

    def test_extract_reset_and_judge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            manifest = extract(Path("logs/task.log"), root)
            self.assertEqual(len(manifest["task_order"]), 6)
            env = LocalTaskEnvironment(root, "task_1_alpha.md")
            phase = env.reset()
            self.assertTrue(phase.startswith("请阅读") and Path(env.task_path).exists())
            self.assertFalse(env.submit(json.dumps(env.expected_answer())).accepted)
            workspace = env.runtime / "ws_1"
            (workspace / "logs/alpha").mkdir(parents=True)
            (workspace / "logs/alpha").chmod(0o755)
            lines = (workspace / "config/alpha.conf").read_text().splitlines()
            lines[2], lines[5] = "port 8080", "name alpha-app"
            (workspace / "config/alpha.conf").write_text("\n".join(lines) + "\n")
            (workspace / "bin/start.sh").chmod(0o755)
            self.assertTrue(env.submit(json.dumps(env.expected_answer())).accepted)
            env.close()

    def test_api_judge_is_derived_from_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            extract(Path("logs/task.log"), root)
            env = LocalTaskEnvironment(root, "task_2_nanjing.md")
            env.reset()
            answer = env.expected_answer()
            answer["types"].reverse()
            self.assertTrue(env.submit(json.dumps(answer, ensure_ascii=False)).accepted)
            answer["total_count"] = "3"
            self.assertFalse(env.submit(json.dumps(answer, ensure_ascii=False)).accepted)
            env.close()

    def test_local_api_reproduces_stale_document_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            extract(Path("logs/task.log"), root)
            env = LocalTaskEnvironment(root, "task_1_beijing.md")
            env.reset()
            env.start_services()
            url = "http://127.0.0.1:8899/api/v1/heritage/search?location=%E5%8C%97%E4%BA%AC&offset=1&limit=1"
            with self.assertRaises(HTTPError) as error:
                urlopen(url)
            self.assertEqual(error.exception.code, 401)
            request = Request(url, headers={"Authorization": "Bearer heritage-api-key-2024"})
            with urlopen(request) as response:
                payload = json.load(response)
            self.assertEqual(payload["code"], 200)
            self.assertEqual(len(payload["data"]["records"]), 1)
            self.assertEqual(payload["data"]["pagination"]["offset"], 1)
            self.assertNotIn("era_order", payload["data"]["records"][0])
            request = Request(
                "http://127.0.0.1:8899/api/v1/heritage/search?location=%E5%8C%97%E4%BA%AC",
                headers={"Authorization": "Bearer heritage-api-key-2024"})
            with urlopen(request) as response:
                first_page = json.load(response)
            self.assertEqual(first_page["data"]["pagination"]["total_count"], 15)
            self.assertEqual(len(first_page["data"]["records"]), 10)
            env.close()
