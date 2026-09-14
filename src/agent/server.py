"""HTTP服务与请求处理。"""
import itertools
import json
from copy import deepcopy
from time import perf_counter
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, request

from .protocol import MatchState
from .task_logging import task_diagnostics
from .news_logging import news_diagnostics
from .task_solver import PioneerTaskSolver
from .news_memory import NewsMemory
from .prompt_router import PromptRouter
from .brain import V1Strategy, BasicActionValidator, is_day_round
from .decision_log import snapshot, build_report, write_report


def load_build_memory(state: "MatchState", state_dir: Path) -> None:
    """从state/build_memory.json恢复跨回合学习记忆。"""
    if state.memory_loaded:
        return
    state.memory_loaded = True
    path = state_dir / "build_memory.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    state.memory_context = data.get("memory_context")
    state.memory_round = data.get("memory_round")
    state.build_retry_after = {tuple(entry[:3]): entry[3] for entry in data.get("build_retry_after", [])}
    state.failed_build_spots = {tuple(p) for p in data.get("failed_build_spots", [])}
    state.worker_build_targets = {
        int(role_id): tuple(value) for role_id, value in data.get("worker_build_targets", {}).items()
    }
    state.worker_item_jobs = {}
    for role_id, job in data.get("worker_item_jobs", {}).items():
        job = dict(job)
        job["target"] = tuple(job["target"])
        state.worker_item_jobs[int(role_id)] = job
    state.last_sent_command = {
        int(role_id): cmd for role_id, cmd in data.get("last_sent_command", {}).items()
    }


def save_build_memory(state: "MatchState", state_dir: Path) -> None:
    """落盘跨回合学习记忆，服务重启后可恢复。"""
    if (
        not state.failed_build_spots
        and not state.worker_build_targets
        and not state.worker_item_jobs
        and not state.last_sent_command
        and state.memory_context is None
        and not (state_dir / "build_memory.json").exists()
    ):
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "build_memory.json"
    data = {
        "memory_context": state.memory_context,
        "memory_round": state.memory_round,
        "build_retry_after": [[*key, value] for key, value in state.build_retry_after.items()],
        "failed_build_spots": [list(pos) for pos in state.failed_build_spots],
        "worker_build_targets": {
            str(role_id): list(value)
            for role_id, value in state.worker_build_targets.items()
        },
        "worker_item_jobs": {
            str(role_id): {**job, "target": list(job["target"])}
            for role_id, job in state.worker_item_jobs.items()
        },
        "last_sent_command": {str(role_id): cmd for role_id, cmd in state.last_sent_command.items()},
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


class GameServer:
    """游戏HTTP服务器，管理请求响应周期与跨回合持久化。"""

    def __init__(self, root_dir: Path, strategy=None):
        self._request_lock = Lock()
        self.root = root_dir
        self.log_dir = root_dir / "logs"
        self.state_dir = root_dir / "state"
        self.request_sequence = itertools.count(1)
        self.match_state = MatchState()
        self.previous_snapshot = None
        self.strategy = strategy or V1Strategy(BasicActionValidator())
        self.task_solver = PioneerTaskSolver(self.state_dir)
        self.news_memory = NewsMemory(self.state_dir)
        self.prompt_router = PromptRouter(self.news_memory)
        self.app = Flask(__name__)
        self._setup_routes()

    def _setup_routes(self):
        @self.app.route("/", methods=["POST"])
        def process_request():
            with self._request_lock:
                return self._handle_request()

    def prepare_directories(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def load_build_memory(self) -> None:
        """从state/build_memory.json恢复跨回合学习记忆。"""
        load_build_memory(self.match_state, self.state_dir)

    def save_build_memory(self) -> None:
        """落盘跨回合学习记忆，服务重启后可恢复。"""
        save_build_memory(self.match_state, self.state_dir)

    def _handle_request(self):
        """处理单次游戏请求。"""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid JSON object"}), 400
        try:
            self.prepare_directories()
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            while True:
                seq = next(self.request_sequence)
                log_path = self.log_dir / f"request_{seq:06d}.json"
                try:
                    with log_path.open("x", encoding="utf-8") as stream:
                        stream.write(payload)
                    break
                except FileExistsError:
                    continue

            # 策略决策
            self.load_build_memory()
            self.match_state.update(data)
            self.match_state.news_memory = self.news_memory
            # 先清空再 ingest/consume，避免解码 trace 被冲掉
            self.match_state.decision_events = []
            self.news_memory.ingest(self.match_state)
            self.prompt_router.consume_llm_resp(self.match_state)
            previous_commands = deepcopy(self.match_state.last_sent_command)
            before = snapshot(self.match_state)
            started = perf_counter()
            role_command_map = self.strategy.decide(self.match_state)
            prompt, execute_cmd = self.task_solver.step(self.match_state, role_command_map)
            news_prompt = self.prompt_router.request_prompt(self.match_state)
            prompt = prompt or news_prompt
            diagnostic_cmd = task_diagnostics(
                self.match_state, role_command_map, previous_commands,
                self.task_solver.session.get('stage', 'idle'),
            )
            news_cmd = news_diagnostics(
                self.match_state, self.news_memory, role_command_map, previous_commands,
            )
            # 与自进化一致：解题命令优先；否则任务诊断；再否则新闻诊断经沙盒回传。
            execute_cmd = execute_cmd or diagnostic_cmd or news_cmd
            elapsed_ms = (perf_counter() - started) * 1000
            # 诊断日志失败不应让合法比赛响应变成500。
            try:
                report = build_report(
                    self.match_state, role_command_map, previous_commands, before,
                    self.previous_snapshot, seq, elapsed_ms,
                    "未知" if self.match_state.round_no is None else (
                        "白天" if is_day_round(self.match_state.round_no) else "夜晚"),
                )
                write_report(self.log_dir, report)
            except Exception:
                self.app.logger.exception("decision logging failed (response unaffected)")
            self.previous_snapshot = before
            self.save_build_memory()

            command = {"roleCommandMap": role_command_map, "prompt": prompt, "executeCmd": execute_cmd}

            response_path = self.log_dir / f"response_{seq:06d}.json"
            try:
                with response_path.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(command, ensure_ascii=False, indent=2))
            except FileExistsError:
                pass
            return jsonify(command), 200
        except Exception:
            self.app.logger.exception("request processing failed")
            return jsonify({"error": "internal server error"}), 500

    def run(self, host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
        self.app.run(host=host, port=port, debug=debug)


def main():
    """Installed console entry point; runtime files default to the working directory."""
    import argparse
    parser = argparse.ArgumentParser(description="Competition HTTP service")
    parser.add_argument("port", type=int)
    parser.add_argument("--data-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    GameServer(args.data_dir).run(port=args.port)
