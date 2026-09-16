"""Local, deterministic task reset and submission judging."""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import Thread
from urllib.parse import parse_qs, urlparse


@dataclass
class JudgeResult:
    accepted: bool
    error_code: int | None = None
    description: str = ""


class LocalHeritageAPIServer:
    """Small NCHDA-compatible server reproducing the stale-doc surprises."""

    def __init__(self, dataset_path: Path, host: str = "127.0.0.1", port: int = 8899):
        self.records = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
        self.host, self.port = host, port
        self.httpd = None
        self.thread = None

    def start(self) -> None:
        records = self.records

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                # curl may send raw UTF-8 in the request target. BaseHTTPRequestHandler
                # initially decodes HTTP bytes as latin-1, so recover UTF-8 before parse_qs.
                request_target = self.path
                try:
                    request_target = request_target.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                parsed = urlparse(request_target)
                if parsed.path != "/api/v1/heritage/search":
                    return self.reply(404, {"status": "error", "message": f"Endpoint not found: {parsed.path}", "code": 404})
                if self.headers.get("Authorization") != "Bearer heritage-api-key-2024":
                    return self.reply(401, {"status": "error", "message":
                        "Authentication failed: Missing 'Authorization' header. Expected format: 'Authorization: Bearer <api_key>'",
                        "code": 401})
                query = parse_qs(parsed.query)
                city = (query.get("location") or [None])[0]
                if not city:
                    return self.reply(400, {"status": "error", "message": "Missing required parameter: location", "code": 400})
                if city not in records:
                    return self.reply(404, {"status": "error", "message": "No records for location", "code": 404})
                try:
                    offset = max(0, int((query.get("offset") or [0])[0]))
                    limit = min(100, max(1, int((query.get("limit") or [10])[0])))
                except ValueError:
                    return self.reply(400, {"status": "error", "message": "Invalid pagination", "code": 400})
                public = [{k: v for k, v in item.items() if k != "era_order"}
                          for item in records[city][offset:offset + limit]]
                return self.reply(200, {"code": 200, "data": {"records": public, "pagination": {
                    "total_count": len(records[city]), "offset": offset, "limit": limit}}})

            def log_message(self, *_):
                return

        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            raise RuntimeError(f"无法启动本地 API {self.host}:{self.port}: {exc}") from exc
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        self.httpd = self.thread = None


class LocalTaskEnvironment:
    def __init__(self, fixtures: Path, task_name: str):
        self.fixtures = Path(fixtures)
        self.task_name = task_name
        self.runtime: Path | None = None
        self.task_path: Path | None = None
        self.api_server: LocalHeritageAPIServer | None = None
        self.kind = "api" if any(x in task_name for x in ("beijing", "nanjing", "chengdu")) else "deployment"

    def reset(self) -> str:
        self.close()
        self.runtime = Path(tempfile.mkdtemp(prefix="local_task_"))
        source_dir = self.fixtures / ("1-unknown-api" if self.kind == "api" else "2-engineering-fix")
        if self.kind == "deployment":
            number = self.task_name.split("_")[1]
            shutil.copytree(source_dir / f"ws_{number}.template", self.runtime / f"ws_{number}")
        self.task_path = self.runtime / self.task_name
        text = (source_dir / self.task_name).read_text(encoding="utf-8")
        # The solver must see paths in this fresh runtime, not stale original /tmp paths.
        if self.kind == "deployment":
            number = self.task_name.split("_")[1]
            old = f"/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/ws_{number}/"
            text = text.replace(old, str(self.runtime / f"ws_{number}") + "/")
        self.task_path.write_text(text, encoding="utf-8")
        if self.kind == "api":
            shutil.copy2(source_dir / "API_DOCS.md", self.runtime / "API_DOCS.md")
        return f"请阅读{self.task_path}，获取任务信息"

    def start_services(self) -> None:
        if self.kind == "api" and self.api_server is None:
            dataset = self.fixtures / "1-unknown-api" / "dataset.json"
            self.api_server = LocalHeritageAPIServer(dataset)
            self.api_server.start()

    def expected_answer(self) -> dict:
        if self.kind == "deployment":
            app = self.task_name.rsplit("_", 1)[1].removesuffix(".md")
            manifest = json.loads((self.fixtures / "manifest.json").read_text(encoding="utf-8"))
            return {"token": manifest["deployment_tokens"][app]}
        city = {"beijing": "北京", "nanjing": "南京", "chengdu": "成都"}[
            self.task_name.rsplit("_", 1)[1].removesuffix(".md")]
        records = json.loads((self.fixtures / "1-unknown-api" / "dataset.json").read_text(encoding="utf-8"))[city]
        oldest = min(records, key=lambda item: item["era_order"])
        return {"city": city, "total_count": len(records),
                "world_heritage_count": sum(r["protected_level"] == "世界遗产" for r in records),
                "types": sorted({r["type"] for r in records}), "oldest_era": oldest["name"]}

    def submit(self, raw_answer: str) -> JudgeResult:
        try:
            actual = json.loads(raw_answer)
        except (TypeError, json.JSONDecodeError):
            return JudgeResult(False, 2, "taskAnswer 不是合法 JSON 字符串")
        expected = self.expected_answer()
        if self.kind == "api" and isinstance(actual, dict):
            actual = dict(actual)
            if isinstance(actual.get("types"), list):
                actual["types"] = sorted(actual["types"])
        if actual != expected:
            return JudgeResult(False, 2, f"答案错误: expected={expected!r}, actual={actual!r}")
        if self.kind == "deployment":
            number = self.task_name.split("_")[1]
            app = self.task_name.rsplit("_", 1)[1].removesuffix(".md")
            workspace = self.runtime / f"ws_{number}"
            spec = (workspace / "spec.md").read_text(encoding="utf-8")
            required = []
            for line in spec.splitlines():
                match = re.search(r"第 \d+ 行：`?([^`\n]+)`?", line)
                if match:
                    required.append(match.group(1).strip())
            config = (workspace / "config" / f"{app}.conf")
            lines = config.read_text(encoding="utf-8").splitlines() if config.is_file() else []
            log_dir, start = workspace / "logs" / app, workspace / "bin" / "start.sh"
            valid = (log_dir.is_dir() and stat.S_IMODE(log_dir.stat().st_mode) == 0o755
                     and len(lines) >= 6 and len(required) == 2
                     and lines[2] == required[0] and lines[5] == required[1]
                     and start.is_file() and stat.S_IMODE(start.stat().st_mode) == 0o755)
            if not valid:
                return JudgeResult(False, 2, "提交 token 正确，但工作区独立复检未通过")
        return JudgeResult(True)

    def close(self) -> None:
        if self.api_server:
            self.api_server.close()
            self.api_server = None
        if self.runtime and self.runtime.exists():
            shutil.rmtree(self.runtime)
        self.runtime = None
        self.task_path = None

    def __enter__(self):
        self.reset()
        return self

    def __exit__(self, *_):
        self.close()
