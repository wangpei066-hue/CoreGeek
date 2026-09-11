"""数据结构与协议定义，对应接口文档1.1-1.7的结构化状态实体。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


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

    每回合请求都是全量快照（接口文档1.1），因此 update() 直接整体重建字段，
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
        # 同时可落盘到 state/build_memory.json，见 load_build_memory/save_build_memory。
        self.last_sent_command = {}
        self.failed_build_spots = set()
        self.worker_build_targets = {}
        self.worker_item_jobs = {}
        self.memory_loaded = False

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
