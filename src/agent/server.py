"""HTTP服务与请求处理。"""
import itertools
import json
import logging
from pathlib import Path

from flask import Flask, jsonify, request

from .protocol import MatchState
from .brain import V1Strategy, BasicActionValidator

LOGGER = logging.getLogger(__name__)


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
    ):
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "build_memory.json"
    data = {
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
        self.root = root_dir
        self.log_dir = root_dir / "logs"
        self.state_dir = root_dir / "state"
        self.request_sequence = itertools.count(1)
        self.match_state = MatchState()
        self.strategy = strategy or V1Strategy(BasicActionValidator())
        self.app = Flask(__name__)
        self._setup_routes()

    def _setup_routes(self):
        @self.app.route("/", methods=["POST"])
        def process_request():
            return self._handle_request()

    def prepare_directories(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def load_build_memory(self) -> None:
        load_build_memory(self.match_state, self.state_dir)

    def save_build_memory(self) -> None:
        save_build_memory(self.match_state, self.state_dir)

    def _handle_request(self):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid JSON object"}), 400
        try:
            self.prepare_directories()
            self.load_build_memory()
            self.match_state.update(data)
            role_command_map = self.strategy.decide(self.match_state)
            self.save_build_memory()

            # Demo 风格：每回合把决策打到 stdout
            LOGGER.info("round %s -> %s", data.get("roundNo"), role_command_map)

            command = {"roleCommandMap": role_command_map, "prompt": "", "executeCmd": ""}
            return jsonify(command), 200
        except Exception:
            LOGGER.exception("decision failed")
            return jsonify({"roleCommandMap": {}, "prompt": "", "executeCmd": ""}), 200

    def run(self, host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
        self.app.run(host=host, port=port, debug=debug)
