"""V1 HTTP service. Requests are parsed into structured GameState and answered by V1Strategy."""
import argparse
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
import heapq
import itertools
import json
from pathlib import Path
import sys
from typing import Optional

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent


def callback(json_data):
    """唯一策略入口：解析状态 -> V1Strategy 生成指令 -> 本地校验后返回。"""
    match_state.update(json_data)
    role_command_map = strategy.decide(match_state)
    return {"roleCommandMap": role_command_map, "prompt": "", "executeCmd": ""}


# SDK transport layer; callback remains the sole strategy entry.
app = Flask(__name__)
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
request_sequence = itertools.count(1)


def prepare_directories():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@app.route("/", methods=["POST"])
def process_request():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid JSON object"}), 400
    try:
        prepare_directories()
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        while True:
            log_path = LOG_DIR / f"request_{next(request_sequence):06d}.json"
            try:
                with log_path.open("x", encoding="utf-8") as stream:
                    stream.write(payload)
                break
            except FileExistsError:
                continue
        command = callback(data)
        return jsonify(command), 200
    except Exception:
        app.logger.exception("request processing failed")
        return jsonify({"error": "internal server error"}), 500




# --- 接口文档 1.1-1.7 对应的结构化状态实体（docs/接口文档.md） ---


@dataclass
class Pos:
    x: int
    y: int

    @classmethod
    def from_dict(cls, data: dict) -> "Pos":
        return cls(x=data["x"], y=data["y"])


@dataclass
class Zone:
    pos: Pos
    neutral_type: str

    @classmethod
    def from_dict(cls, data: dict) -> "Zone":
        return cls(pos=Pos.from_dict(data["pos"]), neutral_type=data["neutralType"])


@dataclass
class MapInfo:
    width: int
    height: int
    zones: list

    @classmethod
    def from_dict(cls, data: dict) -> "MapInfo":
        return cls(
            width=data["width"],
            height=data["height"],
            zones=[Zone.from_dict(z) for z in data.get("zones", [])],
        )


@dataclass
class Role:
    """通用单位属性（角色/建筑）。level、cooldown 仅建筑/武器持有，角色为 None。"""

    id: int
    pos: Pos
    role_type: str
    health: int
    attack_power: int = 0
    attack_range: int = 0
    back_pack_capability: int = 0
    backpack: list = field(default_factory=list)
    level: Optional[int] = None
    cooldown: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Role":
        return cls(
            id=data["id"],
            pos=Pos.from_dict(data["pos"]),
            role_type=data["roleType"],
            health=data["health"],
            attack_power=data.get("attackPower", 0),
            attack_range=data.get("attackRange", 0),
            back_pack_capability=data.get("backPackCapability", 0),
            backpack=list(data.get("backpack", [])),
            level=data.get("level"),
            cooldown=data.get("cooldown"),
        )


@dataclass
class PlayerTask:
    task_type: str
    task_position: Pos
    cold_down_rounds: int
    score_reward: int
    gold_reward: int
    is_valid: bool
    timeout_rounds: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerTask":
        return cls(
            task_type=data["taskType"],
            task_position=Pos.from_dict(data["taskPosition"]),
            cold_down_rounds=data["coldDownRounds"],
            score_reward=data["scoreReward"],
            gold_reward=data["goldReward"],
            is_valid=data["isValid"],
            timeout_rounds=data.get("timeoutRounds"),
        )


@dataclass
class TeamOur:
    type: str
    team_id: str
    team_name: str
    gold_num: int
    total_score: int
    player_tasks: list
    roles: list

    @classmethod
    def from_dict(cls, data: dict) -> "TeamOur":
        return cls(
            type=data.get("type", ""),
            team_id=data.get("teamId", ""),
            team_name=data.get("teamName", ""),
            gold_num=data.get("goldNum", 0),
            total_score=data.get("totalScore", 0),
            player_tasks=[PlayerTask.from_dict(t) for t in data.get("playerTasks", [])],
            roles=[Role.from_dict(r) for r in data.get("roles", [])],
        )


@dataclass
class TeamEnemy:
    roles: list

    @classmethod
    def from_dict(cls, data: dict) -> "TeamEnemy":
        return cls(roles=[Role.from_dict(r) for r in data.get("roles", [])])


@dataclass
class RobotRole:
    id: int
    pos: Pos
    role_type: str
    health: int
    abnormal_state: str = ""
    target_team: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RobotRole":
        return cls(
            id=data["id"],
            pos=Pos.from_dict(data["pos"]),
            role_type=data["roleType"],
            health=data["health"],
            abnormal_state=data.get("abnormalState", ""),
            target_team=data.get("targetTeam", ""),
        )


@dataclass
class RobotInfo:
    roles: list

    @classmethod
    def from_dict(cls, data: dict) -> "RobotInfo":
        return cls(roles=[RobotRole.from_dict(r) for r in data.get("roles", [])])


@dataclass
class WorldNews:
    official_news: str = ""
    folk_legends: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "WorldNews":
        return cls(
            official_news=data.get("officialNews", ""),
            folk_legends=data.get("folkLegends", ""),
        )


@dataclass
class ShopItem:
    name: str
    price: int

    @classmethod
    def from_dict(cls, data: dict) -> "ShopItem":
        return cls(name=data["name"], price=data["price"])


@dataclass
class ErrorInfo:
    error_code: int
    description: str

    @classmethod
    def from_dict(cls, data: dict) -> "ErrorInfo":
        return cls(error_code=data.get("errorCode", 0), description=data.get("description", ""))


class GameState(ABC):
    @abstractmethod
    def update(self, payload: dict) -> None:
        """待实现：根据已核验的真实协议更新状态。"""
        raise NotImplementedError


class MatchState(GameState):
    """P1：把判题器请求快照解析为结构化字段，不做跨回合持久化，不生成指令。

    每回合请求都是全量快照（接口文档 1.1），因此 update() 直接整体重建字段，
    而不是增量合并；跨回合才需要的信息（矿点历史、任务线索、LLM 计数等）留给后续阶段。
    """

    def __init__(self):
        self.round_no = None
        self.map_info = None
        self.team_our = None
        self.team_enemy = None
        self.robot = None
        self.phase_task = ""
        self.last_round_role_action_results = {}
        self.last_summon_treasure_result = 0
        self.llm_resp = ""
        self.world_news = None
        self.last_cmd_result = ""
        self.vendor_shop_list = []
        self.weapon_shop_list = []
        self.errors = []
        # 以下字段跨回合持久化（update() 不会重置），供 V1Strategy 在多回合间学习/记忆使用。
        self.last_sent_command = {}
        self.failed_build_spots = set()
        self.worker_build_targets = {}

    def update(self, payload: dict) -> None:
        self.round_no = payload.get("roundNo")
        self.map_info = MapInfo.from_dict(payload["mapInfo"]) if "mapInfo" in payload else None
        self.team_our = TeamOur.from_dict(payload["teamOur"]) if "teamOur" in payload else None
        self.team_enemy = TeamEnemy.from_dict(payload["teamEnemy"]) if "teamEnemy" in payload else None
        self.robot = RobotInfo.from_dict(payload["robot"]) if "robot" in payload else None
        self.phase_task = payload.get("phaseTask", "")
        self.last_round_role_action_results = {
            int(k): v for k, v in payload.get("lastRoundRoleActionResults", {}).items()
        }
        self.last_summon_treasure_result = payload.get("lastSummonTreasureResult", 0)
        self.llm_resp = payload.get("llmResp", "")
        self.world_news = WorldNews.from_dict(payload.get("worldNews", {}))
        self.last_cmd_result = payload.get("lastCmdResult", "")
        self.vendor_shop_list = [ShopItem.from_dict(i) for i in payload.get("vendorShopList", [])]
        self.weapon_shop_list = [ShopItem.from_dict(i) for i in payload.get("weaponShopList", [])]
        self.errors = [ErrorInfo.from_dict(e) for e in payload.get("errors", [])]


match_state = MatchState()


class Strategy(ABC):
    @abstractmethod
    def decide(self, state: GameState) -> dict:
        """待实现：从状态生成响应。"""
        raise NotImplementedError


class TaskSession(ABC):
    @abstractmethod
    def update(self, payload: dict) -> None:
        """待实现：任务会话生命周期与结果关联。"""
        raise NotImplementedError


class ActionValidator(ABC):
    @abstractmethod
    def validate(self, command: dict, state: GameState) -> None:
        """待实现：根据确认的动作协议及规则校验响应。"""
        raise NotImplementedError


# =====================================================================
# V1 策略实现
#
# 范围：白天经济循环（采矿/贩卖/机会性建造）+ 夜晚武器操控战斗。
# 明确不做（留给后续版本，见 README 已知限制）：
#   - 推理类/长上下文类/自进化类任务（acceptTask/submitAnswer/summonTreasure）
#   - 武器商店消耗品与升级券购买（DizzyWeapon/Bomb/UpgradeVoucher 等）
#   - LLM prompt 与 executeCmd 沙盒调用
# 这些系统依赖任务答案 schema、可建造区精确坐标等尚未核实的材料
# （见 docs/rules_verified.md），先跑通可验证、风险低的经济与战斗闭环。
# =====================================================================

DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
DAY_NIGHT_CYCLE = DAY_ROUNDS + NIGHT_ROUNDS
WEAPON_TYPES = ("gatling", "railgun", "rocket")
MAX_WEAPONS = 3
WEAPON_GOLD_COST = 25
ORE_TYPES = ("stone", "iron", "copper")
BACKPACK_SELL_RATIO = 0.8
BUILD_RING_MIN_RADIUS = 2
BUILD_RING_MAX_RADIUS = 6
_NEIGHBOR_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def is_day_round(round_no) -> bool:
    """白天=70回合、夜晚=60回合（任务书4.2）。roundNo 起始值未经官方确认，
    取模两种起算方式差异仅在边界回合，V1 接受这一已知误差。"""
    if round_no is None:
        return True
    return (round_no % DAY_NIGHT_CYCLE) < DAY_ROUNDS


def chebyshev(a: Pos, b: Pos) -> int:
    return max(abs(a.x - b.x), abs(a.y - b.y))


def neighbors8(pos: Pos, width: int, height: int):
    for dx, dy in _NEIGHBOR_OFFSETS:
        nx, ny = pos.x + dx, pos.y + dy
        if 0 <= nx < width and 0 <= ny < height:
            yield Pos(nx, ny)


def astar_next_step(start: Pos, goal: Pos, blocked: set, width: int, height: int):
    """8 方向 A*（代价 1，启发式为切比雪夫距离），返回从 start 走向 goal 的下一步单格坐标。"""
    if (start.x, start.y) == (goal.x, goal.y):
        return None
    goal_key = (goal.x, goal.y)
    open_heap = [(chebyshev(start, goal), 0, (start.x, start.y))]
    came_from = {(start.x, start.y): None}
    g_score = {(start.x, start.y): 0}
    visited = set()
    while open_heap:
        _, g, current = heapq.heappop(open_heap)
        if current in visited:
            continue
        visited.add(current)
        if current == goal_key:
            node = current
            path = []
            while came_from[node] is not None:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return Pos(*path[0]) if path else None
        for n in neighbors8(Pos(*current), width, height):
            key = (n.x, n.y)
            if key != goal_key and key in blocked:
                continue
            tentative = g + 1
            if tentative < g_score.get(key, 1_000_000_000):
                g_score[key] = tentative
                came_from[key] = current
                heapq.heappush(open_heap, (tentative + chebyshev(n, goal), tentative, key))
    return None


def nearest_adjacent_free_cell(start: Pos, target: Pos, blocked: set, width: int, height: int):
    candidates = [n for n in neighbors8(target, width, height) if (n.x, n.y) not in blocked]
    if not candidates:
        return None
    return min(candidates, key=lambda c: chebyshev(start, c))


def move_towards(start: Pos, target: Pos, blocked: set, width: int, height: int):
    """返回本回合应移动到的下一格；已在 target 一格范围内则返回 None（可直接行动，无需移动）。"""
    if chebyshev(start, target) <= 1:
        return None
    goal = nearest_adjacent_free_cell(start, target, blocked, width, height)
    if goal is None:
        return None
    return astar_next_step(start, goal, blocked, width, height)


def build_blocked_set(state: "MatchState") -> set:
    """阻挡集合：己方/敌方建筑与角色、机器人（任务书4.1）。中立元素（矿区/小贩/商店/任务点）
    与队伍角色分别处理；基地为 2x2，pos 是左上角坐标（接口文档1.3.1注）。"""
    blocked = set()
    if state.map_info:
        for zone in state.map_info.zones:
            blocked.add((zone.pos.x, zone.pos.y))

    def add_role(role: Role):
        if role.role_type == "station":
            bx, by = role.pos.x, role.pos.y
            for dx in (0, 1):
                for dy in (0, 1):
                    blocked.add((bx + dx, by + dy))
        else:
            blocked.add((role.pos.x, role.pos.y))

    if state.team_our:
        for role in state.team_our.roles:
            add_role(role)
    if state.team_enemy:
        for role in state.team_enemy.roles:
            add_role(role)
    if state.robot:
        for robot in state.robot.roles:
            blocked.add((robot.pos.x, robot.pos.y))
    return blocked


def find_zone(state: "MatchState", neutral_type: str):
    if not state.map_info:
        return None
    for zone in state.map_info.zones:
        if zone.neutral_type == neutral_type:
            return zone
    return None


def nearest_mine(state: "MatchState", worker: Role):
    if not state.map_info:
        return None
    candidates = [z for z in state.map_info.zones if z.neutral_type in ORE_TYPES]
    if not candidates:
        return None
    return min(candidates, key=lambda z: chebyshev(worker.pos, z.pos))


def own_station(state: "MatchState"):
    if not state.team_our:
        return None
    return next((r for r in state.team_our.roles if r.role_type == "station"), None)


def _ring_offsets(min_radius=BUILD_RING_MIN_RADIUS, max_radius=BUILD_RING_MAX_RADIUS):
    offsets = []
    for r in range(min_radius, max_radius + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) == r:
                    offsets.append((dx, dy))
    return offsets


_BUILD_RING_OFFSETS = _ring_offsets()


def pick_build_target(state: "MatchState", base_pos: Pos, blocked: set):
    """在基地周围环形扩展搜索一个未阻挡、未被记录为建造失败的候选格。
    精确可建造区坐标未知（docs/rules_verified.md），这里用"离基地由近到远试探 + 失败记忆黑名单"
    的方式经验性逼近，而不是依赖猜测的蓝/黄区范围。"""
    width, height = state.map_info.width, state.map_info.height
    for dx, dy in _BUILD_RING_OFFSETS:
        x, y = base_pos.x + dx, base_pos.y + dy
        if not (0 <= x < width and 0 <= y < height):
            continue
        key = (x, y)
        if key in state.failed_build_spots or key in blocked:
            continue
        return Pos(x, y)
    return None


def pick_weapon_name(state: "MatchState") -> str:
    counts = Counter(r.role_type for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    for name in WEAPON_TYPES:
        if counts.get(name, 0) == 0:
            return name
    return min(WEAPON_TYPES, key=lambda n: counts.get(n, 0))


def learn_from_last_round(state: "MatchState") -> None:
    """用上一回合的执行结果反馈修正建造黑名单：仅当上一回合我方确实发送过 build
    指令且被判定不合法时才拉黑对应坐标，避免误伤其他动作类型的失败。"""
    if not state.last_round_role_action_results or not state.last_sent_command:
        return
    for role_id, success in state.last_round_role_action_results.items():
        if success:
            continue
        prev = state.last_sent_command.get(role_id)
        if not prev or prev.get("action") != "build":
            continue
        for pos in prev.get("targetPos", []):
            state.failed_build_spots.add((pos["x"], pos["y"]))


def try_build(worker: Role, state: "MatchState", blocked: set, reserved: set):
    """机会性建造：优先补齐 3 座武器位，其次消耗背包里的石头建围墙。
    有正在前往的建造目标时先赶路，到达一格内再真正发 build。"""
    base = own_station(state)
    if base is None or state.map_info is None:
        return None

    pending = state.worker_build_targets.get(worker.id)
    if pending:
        x, y, kind = pending
        if (x, y) in state.failed_build_spots:
            del state.worker_build_targets[worker.id]
        else:
            target = Pos(x, y)
            if chebyshev(worker.pos, target) <= 1:
                del state.worker_build_targets[worker.id]
                if kind == "weapon":
                    if state.team_our.gold_num < WEAPON_GOLD_COST:
                        return None
                    name = pick_weapon_name(state)
                else:
                    if "stone" not in worker.backpack:
                        return None
                    name = "wall"
                return {"action": "build", "name": name, "targetPos": [{"x": x, "y": y}]}
            step = move_towards(worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
            if step:
                reserved.add((step.x, step.y))
                return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
            return None

    weapon_count = sum(1 for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    can_weapon = state.team_our.gold_num >= WEAPON_GOLD_COST and weapon_count < MAX_WEAPONS
    can_wall = "stone" in worker.backpack
    if can_weapon:
        kind = "weapon"
    elif can_wall:
        kind = "wall"
    else:
        return None

    target = pick_build_target(state, base.pos, blocked | reserved)
    if target is None:
        return None
    state.worker_build_targets[worker.id] = (target.x, target.y, kind)
    reserved.add((target.x, target.y))
    step = move_towards(worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
    if step:
        reserved.add((step.x, step.y))
        return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def decide_worker_day(worker: Role, state: "MatchState", blocked: set, reserved: set):
    width, height = state.map_info.width, state.map_info.height
    vendor = find_zone(state, "vendor")

    if vendor and worker.backpack and chebyshev(worker.pos, vendor.pos) <= 1:
        name, num = Counter(worker.backpack).most_common(1)[0]
        return {"action": "sell", "name": name, "num": num}

    if vendor and len(worker.backpack) >= int(worker.back_pack_capability * BACKPACK_SELL_RATIO):
        step = move_towards(worker.pos, vendor.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
        return None

    build_cmd = try_build(worker, state, blocked, reserved)
    if build_cmd:
        return build_cmd

    mine = nearest_mine(state, worker)
    if mine:
        if chebyshev(worker.pos, mine.pos) <= 1:
            return {"action": "collect", "targetPos": [{"x": mine.pos.x, "y": mine.pos.y}]}
        step = move_towards(worker.pos, mine.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def plan_day(state: "MatchState") -> dict:
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    blocked = build_blocked_set(state)
    reserved = set()
    for role in state.team_our.roles:
        if role.role_type != "worker":
            continue
        cmd = decide_worker_day(role, state, blocked, reserved)
        if cmd:
            commands[role.id] = cmd
    # 开拓者：V1 暂不实现任务系统（答案 schema 未核实，见 docs/rules_verified.md），白天待机。
    return commands


_ROBOT_PRIORITY = {"bossRobot": 4, "largeRobot": 3, "middleRobot": 2, "smallRobot": 1}


def pick_attack_target(weapon: Role, robots: list):
    in_range = [r for r in robots if chebyshev(weapon.pos, r.pos) <= weapon.attack_range]
    if not in_range:
        return None
    in_range.sort(key=lambda r: (-_ROBOT_PRIORITY.get(r.role_type, 0), r.health))
    return in_range[0]


def target_positions_for_weapon(weapon: Role, target):
    """加特林/火箭的目标位置数须等于武器等级（接口文档2.2）；V1 用同一坐标重复填充，
    保证数量合法，但未真正利用多目标分摊伤害的战术价值（留待后续版本优化）。"""
    count = (weapon.level or 1) if weapon.role_type in ("gatling", "rocket") else 1
    return [{"x": target.pos.x, "y": target.pos.y}] * count


def plan_night(state: "MatchState") -> dict:
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    blocked = build_blocked_set(state)
    reserved = set()
    robots = state.robot.roles if state.robot else []
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES]
    fighters = [r for r in state.team_our.roles if r.role_type in ("worker", "pioneer")]
    used_weapons = set()

    for fighter in fighters:
        weapon = next(
            (w for w in weapons if w.id not in used_weapons and chebyshev(fighter.pos, w.pos) <= 1),
            None,
        )
        if weapon is not None:
            ready = weapon.role_type != "rocket" or (weapon.cooldown or 0) == 0
            target = pick_attack_target(weapon, robots) if ready else None
            if target is not None:
                used_weapons.add(weapon.id)
                commands[weapon.id] = {
                    "action": "attack",
                    "controllerId": str(fighter.id),
                    "targetPos": target_positions_for_weapon(weapon, target),
                }
                continue

        free_weapons = [w for w in weapons if w.id not in used_weapons]
        if free_weapons:
            nearest_weapon = min(free_weapons, key=lambda w: chebyshev(fighter.pos, w.pos))
            step = move_towards(
                fighter.pos, nearest_weapon.pos, blocked | reserved, state.map_info.width, state.map_info.height
            )
            if step:
                reserved.add((step.x, step.y))
                commands[fighter.id] = {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return commands


class BasicActionValidator(ActionValidator):
    """本地二次校验：只拦截能够确定的字段缺失/明显非法组合，降低真实被判"指令错误"的概率。
    校验通过返回 None；不通过抛 ValueError（沿用抽象方法签名的"校验响应"语义）。"""

    _REQUIRES_TARGET_POS = ("move", "build", "remove", "collect")

    def validate(self, command: dict, state: GameState) -> None:
        action = command.get("action")
        if not action:
            raise ValueError("missing action")
        if action in self._REQUIRES_TARGET_POS and not command.get("targetPos"):
            raise ValueError(f"{action} requires targetPos")
        if action == "attack" and (not command.get("targetPos") or not command.get("controllerId")):
            raise ValueError("attack requires targetPos and controllerId")
        if action == "summonTreasure" and (not command.get("targetPos") or not command.get("item")):
            raise ValueError("summonTreasure requires targetPos and item")
        if action == "submitAnswer" and not command.get("taskAnswer"):
            raise ValueError("submitAnswer requires taskAnswer")
        if action in ("sell", "buy") and not command.get("name"):
            raise ValueError(f"{action} requires name")


class V1Strategy(Strategy):
    """V1：白天经济+机会性建造，夜晚武器操控战斗；任务/宝藏/自进化/商店道具系统留待后续版本。"""

    def __init__(self, validator: ActionValidator):
        self.validator = validator

    def decide(self, state: "MatchState") -> dict:
        learn_from_last_round(state)
        if not state.team_our or not state.map_info:
            commands = {}
        elif is_day_round(state.round_no):
            commands = plan_day(state)
        else:
            commands = plan_night(state)
        commands = self._filter_valid(commands, state)
        state.last_sent_command = commands
        return commands

    def _filter_valid(self, commands: dict, state: "MatchState") -> dict:
        valid = {}
        for role_id, command in commands.items():
            try:
                self.validator.validate(command, state)
            except ValueError:
                continue
            valid[role_id] = command
        return valid


strategy = V1Strategy(BasicActionValidator())


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description="P0 competition HTTP service")
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    prepare_directories()
    app.run(host="0.0.0.0", port=args.port, debug=False)
