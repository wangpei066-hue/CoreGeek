"""V1策略实现：白天经济循环+夜晚武器操控战斗。"""
from collections import Counter
from typing import Optional

from .protocol import (
    ActionValidator, GameState, MatchState, Pos, Role, Strategy
)
from .grid import build_blocked_set, chebyshev, move_towards, nearest_adjacent_free_cell


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

WALL_FIXER_GOLD_COST = 10
WALL_REPAIR_RATIO = 0.8
_WEAPON_STATION_VOUCHER_COST = {1: 100, 2: 150}
_WALL_VOUCHER_COST = {1: 20, 2: 30}
_JOB_KIND_ROLE_TYPES = {"weapon": WEAPON_TYPES, "wall": ("wall",), "station": ("station",)}

MAX_HEALTH = {
    "station": {1: 1500, 2: 3000, 3: 4500},
    "gatling": {1: 1000, 2: 1500, 3: 2000},
    "railgun": {1: 1000, 2: 1500, 3: 2000},
    "rocket": {1: 1000, 2: 1500, 3: 2000},
    "wall": {1: 1000, 2: 1500, 3: 2000},
    "worker": {None: 220},
    "pioneer": {None: 200},
}
HEAL_HP_RATIO = 0.5
MEDICINE_GOLD_COST = 10

_ROBOT_PRIORITY = {"bossRobot": 4, "largeRobot": 3, "middleRobot": 2, "smallRobot": 1}


def is_day_round(round_no) -> bool:
    """白天70回合、夜晚60回合（任务书4.2）。roundNo起始值未经官方确认，
    取模两种起算方式差异仅在边界回合，V1接受这一已知误差。"""
    if round_no is None:
        return True
    return (round_no % DAY_NIGHT_CYCLE) < DAY_ROUNDS


def _ring_offsets(min_radius=BUILD_RING_MIN_RADIUS, max_radius=BUILD_RING_MAX_RADIUS):
    offsets = []
    for r in range(min_radius, max_radius + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) == r:
                    offsets.append((dx, dy))
    return offsets


_BUILD_RING_OFFSETS = _ring_offsets()


def find_zone(state: "MatchState", neutral_type: str):
    if not state.map_info:
        return None
    for zone in state.map_info.zones:
        if zone.neutral_type == neutral_type:
            return zone
    return None


def nearest_mine(state: "MatchState", worker: Role, preferred_types=None):
    if not state.map_info:
        return None
    candidates = [z for z in state.map_info.zones if z.neutral_type in ORE_TYPES]
    if not candidates:
        return None
    if preferred_types:
        preferred = [z for z in candidates if z.neutral_type in preferred_types]
        if preferred:
            candidates = preferred
    return min(candidates, key=lambda z: chebyshev(worker.pos, z.pos))


def own_station(state: "MatchState"):
    if not state.team_our:
        return None
    return next((r for r in state.team_our.roles if r.role_type == "station"), None)


def assign_worker_specialties(state: "MatchState") -> dict:
    """两个工人按 id 分工：较小的专建炮台，较大的专建围墙。只活一人时两者都做。"""
    if not state.team_our:
        return {}
    workers = sorted(
        (r for r in state.team_our.roles if r.role_type == "worker"),
        key=lambda r: r.id,
    )
    if not workers:
        return {}
    if len(workers) == 1:
        return {workers[0].id: "both"}
    mapping = {workers[0].id: "weapon", workers[-1].id: "wall"}
    for extra in workers[1:-1]:
        mapping[extra.id] = "wall"
    return mapping


def is_base_top_left(state: "MatchState", base_pos: Pos) -> bool:
    """左半图视为左上出生点，右半图视为右下出生点。"""
    return base_pos.x < state.map_info.width / 2


def _wall_direction_score(dx: int, dy: int, prefer_right: bool) -> int:
    """优先朝敌方一侧延伸；同侧内更靠外、更贴近基地高度的格子优先。"""
    side = dx if prefer_right else -dx
    return side * 10 - abs(dy)


def _in_preferred_wall_side(dx: int, dy: int, prefer_right: bool) -> bool:
    """左上基地铺右侧上下，右下基地铺左侧上下。"""
    if prefer_right:
        return dx >= 0
    return dx <= 0


def pick_build_target(state: "MatchState", base_pos: Pos, blocked: set) -> Optional[Pos]:
    """在基地周围环形扩展搜索一个未阻挡、未被记录为建造失败的候选格。"""
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


def pick_wall_target(state: "MatchState", base_pos: Pos, blocked: set) -> Optional[Pos]:
    """围墙朝向敌方竖边：左上基地优先右侧上下，右下基地优先左侧上下；先近后远。"""
    prefer_right = is_base_top_left(state, base_pos)
    width, height = state.map_info.width, state.map_info.height
    preferred = []
    fallback = []
    for dx, dy in _BUILD_RING_OFFSETS:
        x, y = base_pos.x + dx, base_pos.y + dy
        if not (0 <= x < width and 0 <= y < height):
            continue
        key = (x, y)
        if key in state.failed_build_spots or key in blocked:
            continue
        radius = max(abs(dx), abs(dy))
        score = _wall_direction_score(dx, dy, prefer_right)
        item = (radius, -score, x, y)
        if _in_preferred_wall_side(dx, dy, prefer_right):
            preferred.append(item)
        else:
            fallback.append(item)
    for bucket in (preferred, fallback):
        if not bucket:
            continue
        bucket.sort()
        _, _, x, y = bucket[0]
        return Pos(x, y)
    return None


def pick_weapon_name(state: "MatchState") -> str:
    counts = Counter(r.role_type for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    for name in WEAPON_TYPES:
        if counts.get(name, 0) == 0:
            return name
    return min(WEAPON_TYPES, key=lambda n: counts.get(n, 0))


def voucher_for(kind: str, level: int):
    """返回(物品名, 金币价格)。kind为weapon/station时用Weapon../Station..Voucher，
    wall时用WallUpgradeVoucher，价格来自任务书4.6.3的"建筑升级券"价目表。"""
    if kind == "wall":
        name = "WallUpgradeVoucher1" if level == 1 else "WallUpgradeVoucher2"
        cost = _WALL_VOUCHER_COST[1 if level == 1 else 2]
    else:
        prefix = "Weapon" if kind == "weapon" else "Station"
        name = f"{prefix}UpgradeVoucher1" if level == 1 else f"{prefix}UpgradeVoucher2"
        cost = _WEAPON_STATION_VOUCHER_COST[1 if level == 1 else 2]
    return name, cost


def max_health(role: Role) -> int:
    table = MAX_HEALTH.get(role.role_type)
    if not table:
        return role.health
    return table.get(role.level, next(iter(table.values())))


def decide_self_heal(role: Role):
    """生命药剂使用者回满血、无需目标坐标（任务书4.6.3）；优先级高于经济/战斗动作。"""
    if "Medicine" not in role.backpack:
        return None
    if role.health >= max_health(role) * HEAL_HP_RATIO:
        return None
    return {"action": "use", "name": "Medicine"}


def decide_buy_medicine(role: Role, state: "MatchState"):
    """机会性补给：路过武器商店且背包没药时顺手买一瓶，不为此专门跑路。"""
    shop = find_zone(state, "weaponShop")
    if not shop or chebyshev(role.pos, shop.pos) > 1:
        return None
    if "Medicine" in role.backpack:
        return None
    if state.team_our.gold_num < MEDICINE_GOLD_COST:
        return None
    if len(role.backpack) >= role.back_pack_capability:
        return None
    return {"action": "buy", "name": "Medicine", "num": 1}


def _pending_item_job_targets(state: "MatchState") -> set:
    return {tuple(job["target"]) for job in state.worker_item_jobs.values()}


def _pick_damaged_wall(state: "MatchState", pending_targets: set):
    candidates = [
        r for r in state.team_our.roles
        if r.role_type == "wall"
        and (r.pos.x, r.pos.y) not in pending_targets
        and r.health < max_health(r) * WALL_REPAIR_RATIO
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.health)


def _pick_upgradeable(state: "MatchState", role_types, pending_targets: set, min_health_ratio: float = 0.0):
    candidates = [
        r for r in state.team_our.roles
        if r.role_type in role_types
        and (r.pos.x, r.pos.y) not in pending_targets
        and (r.level or 1) < 3
        and r.health >= max_health(r) * min_health_ratio
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.level or 1)


def maybe_start_shop_item_job(role: Role, state: "MatchState") -> None:
    """给空闲角色机会性分配一个"买道具->用道具"任务。"""
    if role.id in state.worker_item_jobs or not state.team_our:
        return
    pending_targets = _pending_item_job_targets(state)

    damaged_wall = _pick_damaged_wall(state, pending_targets)
    if damaged_wall and state.team_our.gold_num >= WALL_FIXER_GOLD_COST:
        state.worker_item_jobs[role.id] = {
            "item": "WallFixer", "target": (damaged_wall.pos.x, damaged_wall.pos.y), "kind": "wall",
        }
        return

    station = own_station(state)
    if station and (station.level or 1) < 3 and (station.pos.x, station.pos.y) not in pending_targets:
        name, cost = voucher_for("station", station.level or 1)
        if state.team_our.gold_num >= cost:
            state.worker_item_jobs[role.id] = {"item": name, "target": (station.pos.x, station.pos.y), "kind": "station"}
            return

    weapon = _pick_upgradeable(state, WEAPON_TYPES, pending_targets)
    if weapon:
        name, cost = voucher_for("weapon", weapon.level or 1)
        if state.team_our.gold_num >= cost:
            state.worker_item_jobs[role.id] = {"item": name, "target": (weapon.pos.x, weapon.pos.y), "kind": "weapon"}
            return

    wall = _pick_upgradeable(state, ("wall",), pending_targets, min_health_ratio=WALL_REPAIR_RATIO)
    if wall:
        name, cost = voucher_for("wall", wall.level or 1)
        if state.team_our.gold_num >= cost:
            state.worker_item_jobs[role.id] = {"item": name, "target": (wall.pos.x, wall.pos.y), "kind": "wall"}


def _job_target_still_exists(state: "MatchState", job: dict) -> bool:
    x, y = job["target"]
    role_types = _JOB_KIND_ROLE_TYPES.get(job.get("kind"), ())
    return any(r.pos.x == x and r.pos.y == y and r.role_type in role_types for r in state.team_our.roles)


def decide_shop_item_job(role: Role, state: "MatchState", blocked: set, reserved: set):
    """推进一个已分配的两段式任务：没道具先去商店买，有道具就走到目标建筑一格内使用。"""
    job = state.worker_item_jobs.get(role.id)
    if not job:
        return None
    if not _job_target_still_exists(state, job):
        del state.worker_item_jobs[role.id]
        return None

    width, height = state.map_info.width, state.map_info.height
    item = job["item"]
    x, y = job["target"]
    target = Pos(x, y)

    if item in role.backpack:
        if chebyshev(role.pos, target) <= 1:
            del state.worker_item_jobs[role.id]
            return {"action": "use", "name": item, "targetPos": [{"x": x, "y": y}]}
        step = move_towards(role.pos, target, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
        return None

    shop = find_zone(state, "weaponShop")
    if shop is None:
        del state.worker_item_jobs[role.id]
        return None
    if chebyshev(role.pos, shop.pos) <= 1:
        if len(role.backpack) >= role.back_pack_capability:
            del state.worker_item_jobs[role.id]
            return None
        return {"action": "buy", "name": item, "num": 1}
    step = move_towards(role.pos, shop.pos, blocked | reserved, width, height)
    if step:
        reserved.add((step.x, step.y))
        return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def learn_from_last_round(state: "MatchState") -> None:
    """用上一回合的执行结果反馈修正建造黑名单。"""
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


def _allowed_build_kinds(worker: Role, state: "MatchState", specialty: str):
    weapon_count = sum(1 for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    can_weapon = state.team_our.gold_num >= WEAPON_GOLD_COST and weapon_count < MAX_WEAPONS
    can_wall = "stone" in worker.backpack
    if specialty == "weapon":
        return ("weapon",) if can_weapon else ()
    if specialty == "wall":
        return ("wall",) if can_wall else ()
    kinds = []
    if can_weapon:
        kinds.append("weapon")
    if can_wall:
        kinds.append("wall")
    return tuple(kinds)


def try_build(worker: Role, state: "MatchState", blocked: set, reserved: set, specialty: str = "both"):
    """按工人分工建造：炮台工只建武器，围墙工只建围墙；单人时先炮后墙。"""
    base = own_station(state)
    if base is None or state.map_info is None:
        return None

    allowed = _allowed_build_kinds(worker, state, specialty)
    pending = state.worker_build_targets.get(worker.id)
    if pending:
        x, y, kind = pending
        if (x, y) in state.failed_build_spots or kind not in allowed:
            del state.worker_build_targets[worker.id]
            pending = None
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

    if not allowed:
        return None
    kind = allowed[0]
    picker = pick_wall_target if kind == "wall" else pick_build_target
    target = picker(state, base.pos, blocked | reserved)
    if target is None:
        return None
    state.worker_build_targets[worker.id] = (target.x, target.y, kind)
    reserved.add((target.x, target.y))
    step = move_towards(worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
    if step:
        reserved.add((step.x, step.y))
        return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    if chebyshev(worker.pos, target) <= 1:
        del state.worker_build_targets[worker.id]
        name = pick_weapon_name(state) if kind == "weapon" else "wall"
        return {"action": "build", "name": name, "targetPos": [{"x": target.x, "y": target.y}]}
    return None


def _sellable_ores(worker: Role, specialty: str):
    ores = [item for item in worker.backpack if item in ORE_TYPES]
    if specialty != "wall":
        return ores
    return [item for item in ores if item != "stone"]


def decide_worker_day(worker: Role, state: "MatchState", blocked: set, reserved: set, specialty: str = "both"):
    heal_cmd = decide_self_heal(worker)
    if heal_cmd:
        return heal_cmd

    width, height = state.map_info.width, state.map_info.height
    vendor = find_zone(state, "vendor")
    sellable = _sellable_ores(worker, specialty)
    ore_in_backpack = [item for item in worker.backpack if item in ORE_TYPES]

    if vendor and sellable and chebyshev(worker.pos, vendor.pos) <= 1:
        name, num = Counter(sellable).most_common(1)[0]
        return {"action": "sell", "name": name, "num": num}

    buy_cmd = decide_buy_medicine(worker, state)
    if buy_cmd:
        return buy_cmd

    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    build_cmd = try_build(worker, state, blocked, reserved, specialty)
    if build_cmd:
        return build_cmd

    cargo_for_sell_trip = sellable if specialty == "wall" else ore_in_backpack
    if vendor and len(cargo_for_sell_trip) >= int(worker.back_pack_capability * BACKPACK_SELL_RATIO):
        step = move_towards(worker.pos, vendor.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
        return None

    maybe_start_shop_item_job(worker, state)
    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    preferred_ores = ("stone",) if specialty == "wall" else ("iron", "copper")
    mine = nearest_mine(state, worker, preferred_ores if specialty != "both" else None)
    if mine:
        if chebyshev(worker.pos, mine.pos) <= 1:
            return {"action": "collect", "targetPos": [{"x": mine.pos.x, "y": mine.pos.y}]}
        step = move_towards(worker.pos, mine.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def decide_pioneer_task(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """返回(是否接管角色, 指令)；任务期间昼夜均保持位置。"""
    if pioneer.health <= 0:
        return True, None
    if state.phase_task:
        return True, decide_self_heal(pioneer)
    candidates = sorted(
        (t for t in state.team_our.player_tasks
         if t.task_type in ("自进化类1", "自进化类2")
         and t.is_valid and t.cold_down_rounds == 0),
        key=lambda t: (chebyshev(pioneer.pos, t.task_position), t.task_type),
    )
    if not candidates:
        return False, None
    heal = decide_self_heal(pioneer)
    if heal:
        return True, heal
    for task in candidates:
        if chebyshev(pioneer.pos, task.task_position) <= 1:
            return True, {"action": "acceptTask"}
        step = move_towards(pioneer.pos, task.task_position, blocked | reserved,
                            state.map_info.width, state.map_info.height)
        if step:
            reserved.add((step.x, step.y))
            return True, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return True, None


def decide_pioneer_day(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """任务优先，其余时间执行补给和升级。"""
    handled, command = decide_pioneer_task(pioneer, state, blocked, reserved)
    if handled:
        return command
    heal_cmd = decide_self_heal(pioneer)
    if heal_cmd:
        return heal_cmd

    buy_cmd = decide_buy_medicine(pioneer, state)
    if buy_cmd:
        return buy_cmd

    item_job_cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    maybe_start_shop_item_job(pioneer, state)
    return decide_shop_item_job(pioneer, state, blocked, reserved)


def plan_day(state: "MatchState") -> dict:
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    blocked = build_blocked_set(state)
    reserved = set()
    specialties = assign_worker_specialties(state)
    for role in state.team_our.roles:
        if role.role_type == "worker":
            cmd = decide_worker_day(
                role, state, blocked, reserved, specialties.get(role.id, "both")
            )
        elif role.role_type == "pioneer":
            cmd = decide_pioneer_day(role, state, blocked, reserved)
        else:
            continue
        if cmd:
            commands[role.id] = cmd
    return commands


def pick_attack_target(weapon: Role, robots: list):
    in_range = [r for r in robots if chebyshev(weapon.pos, r.pos) <= weapon.attack_range]
    if not in_range:
        return None
    in_range.sort(key=lambda r: (-_ROBOT_PRIORITY.get(r.role_type, 0), r.health))
    return in_range[0]


def target_positions_for_weapon(weapon: Role, target):
    """加特林/火箭的目标位置数须等于武器等级（接口文档2.2）。"""
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
        if fighter.role_type == "pioneer":
            handled, command = decide_pioneer_task(fighter, state, blocked, reserved)
            if handled:
                if command:
                    commands[fighter.id] = command
                continue
        heal_cmd = decide_self_heal(fighter)
        if heal_cmd:
            commands[fighter.id] = heal_cmd
            continue

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
    """本地二次校验：只拦截能够确定的字段缺失/明显非法组合。"""

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
        if action in ("sell", "buy", "use") and not command.get("name"):
            raise ValueError(f"{action} requires name")


class V1Strategy(Strategy):
    """V1：白天经济+机会性建造，夜晚武器操控战斗。"""

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
