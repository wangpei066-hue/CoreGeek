#!/usr/bin/env python3
"""Drive GameServer round-by-round with an injectable LLM callback."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

from src.agent.local_task_env import LocalTaskEnvironment
from src.agent.server import GameServer


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.6-27b"
PLATFORM_COMMAND_TIMEOUT = 15
PLATFORM_OUTPUT_LIMIT = 64 * 1024


def run_platform_command(command: str, cwd: Path) -> str:
    """Model the executeCmd transport contract from docs/接口文档.md."""
    try:
        proc = subprocess.run(["sh", "-c", command], cwd=cwd, capture_output=True,
                              timeout=PLATFORM_COMMAND_TIMEOUT)
        raw = proc.stdout + proc.stderr
        header = f"[exitCode:{proc.returncode}]\n"
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or b"") + (exc.stderr or b"")
        header = "[TIMEOUT]\n"
    text = raw.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) > PLATFORM_OUTPUT_LIMIT:
        encoded = text.encode("utf-8")[:PLATFORM_OUTPUT_LIMIT]
        text = encoded.decode("utf-8", errors="ignore") + "\n[TRUNCATED]"
    return header + text


class DashScopeLLM:
    """OpenAI-compatible DashScope client; credentials never enter traces."""

    def __init__(self, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, enable_thinking: bool = False):
        key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            raise RuntimeError("缺少 DASHSCOPE_API_KEY；请在当前 shell 中导出新密钥")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("缺少 openai SDK；请安装 requirements.txt") from exc
        self.client = OpenAI(api_key=key, base_url=base_url)
        self.model = model
        self.enable_thinking = enable_thinking

    def __call__(self, prompt: str) -> str:
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"enable_thinking": self.enable_thinking},
            stream=True,
        )
        answer = []
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            content = getattr(choices[0].delta, "content", None)
            if content:
                answer.append(content)
        text = "".join(answer).strip()
        if not text:
            raise RuntimeError("LLM 流结束但未返回 content")
        return text


class LocalTaskDriver:
    def __init__(self, env: LocalTaskEnvironment, llm: Callable[[str], str], max_rounds: int = 40,
                 state_root: Path | None = None, start_round: int = 1):
        self.env, self.llm, self.max_rounds = env, llm, max_rounds
        self.start_round = start_round
        self._owns_state_root = state_root is None
        self.state_root = Path(tempfile.mkdtemp(prefix="local_solver_state_")) if state_root is None else Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.server = GameServer(self.state_root)
        self.client = self.server.app.test_client()
        self.trace = []

    def run(self) -> dict:
        payload = json.loads(Path("tests/fixtures/sample_match_state.json").read_text(encoding="utf-8"))
        payload.update(roundNo=self.start_round, phaseTask=self.env.reset(), llmResp="", lastCmdResult="",
                       errors=[], lastRoundRoleActionResults={})
        self.env.start_services()
        task_timeout = self.max_rounds
        for item in payload["teamOur"].get("playerTasks", []):
            item["timeoutRounds"] = task_timeout
        payload["teamOur"]["roles"] = [r for r in payload["teamOur"]["roles"] if r["roleType"] == "pioneer"]
        for _ in range(self.max_rounds):
            response = self.client.post("/", json=payload)
            if response.status_code != 200:
                raise RuntimeError(response.get_data(as_text=True))
            action = response.get_json()
            entry = {"round": payload["roundNo"], "prompt": action.get("prompt", ""),
                     "executeCmd": action.get("executeCmd", ""), "commands": action.get("roleCommandMap", {})}
            payload.update(llmResp="", lastCmdResult="", errors=[], lastRoundRoleActionResults={})
            command = next((cmd for cmd in action.get("roleCommandMap", {}).values()
                            if cmd.get("action") == "submitAnswer"), None)
            if command:
                judged = self.env.submit(command["taskAnswer"])
                # A syntactically valid submitAnswer is legal even when its answer is wrong.
                payload["lastRoundRoleActionResults"] = {"10011": True}
                if not judged.accepted:
                    payload["errors"] = [{"errorCode": judged.error_code, "description": judged.description}]
                entry["judge"] = judged.__dict__
                self.trace.append(entry)
                if judged.accepted:
                    return {"success": True, "rounds": len(self.trace),
                            "start_round": self.start_round, "end_round": payload["roundNo"], "trace": self.trace}
            elif action.get("executeCmd"):
                payload["lastCmdResult"] = run_platform_command(action["executeCmd"], self.env.runtime)
                entry["toolResult"] = payload["lastCmdResult"]
            elif action.get("prompt"):
                payload["llmResp"] = self.llm(action["prompt"])
                entry["llmResp"] = payload["llmResp"]
            self.trace.append(entry)
            payload["roundNo"] += 1
        return {"success": False, "rounds": self.max_rounds, "trace": self.trace,
                "start_round": self.start_round, "end_round": payload["roundNo"] - 1,
                "error": "task timeout", "errorCode": 1}

    def close(self) -> None:
        self.env.close()
        if self._owns_state_root and self.state_root.exists():
            shutil.rmtree(self.state_root)


def replay_llm(responses: list[str]):
    iterator = iter(responses)
    return lambda _prompt: next(iterator)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one local task with DashScope's OpenAI-compatible API")
    parser.add_argument("task")
    parser.add_argument("--fixtures", type=Path, default=Path("/tmp/selfEvolutionTask/1-fixed-step"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--thinking", action="store_true", help="启用百炼 thinking 模式（默认关闭）")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    env = LocalTaskEnvironment(args.fixtures, args.task)
    try:
        llm = DashScopeLLM(base_url=args.base_url, model=args.model,
                           enable_thinking=args.thinking)
    except RuntimeError as exc:
        parser.error(str(exc))
    driver = LocalTaskDriver(env, llm, max_rounds=args.max_rounds)
    try:
        result = driver.run()
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.trace:
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            args.trace.write_text(rendered, encoding="utf-8")
        print(json.dumps({"success": result["success"], "rounds": result["rounds"],
                          "trace": str(args.trace) if args.trace else None}, ensure_ascii=False))
        raise SystemExit(0 if result["success"] else 1)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
