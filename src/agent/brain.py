"""V1策略实现：白天按 Demo 固定炮台/围墙蓝图建造，夜晚武器操控战斗。"""
from collections import Counter
from typing import Optional

from .protocol import (
    ActionValidator, GameState, MatchState, Pos, Role, Strategy
)
from .grid import (
    build_blocked_set,
    chebyshev,
    footprint_distance,
    is_land_cell,
    move_towards,
    nearest_adjacent_free_cell,
    station_footprint,
)


DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
DAY_NIGHT_CYCLE = DAY_ROUNDS + NIGHT_ROUNDS
WEAPON_TYPES = ("gatling", "railgun", "rocket")
TOWER_LOADOUT = ("gatling", "railgun", "rocket")
MAX_WEAPONS = 3
WEAPON_GOLD_COST = 25
ORE_TYPES = ("stone", "iron", "copper")
WALL_MATERIAL = "stone"
STONE_BATCH = 6
BACKPACK_SELL_RATIO = 0.8

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
_NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def is_day_round(round_no) -> bool:
    """白天70回合、夜晚60回合（任务书4.2）。roundNo起始值未经官方确认，
    取模两种起算方式差异仅在边界回合，V1接受这一已知误差。"""
    if round_no is None:
        return True
    return (round_no % DAY_NIGHT_CYCLE) < DAY_ROUNDS


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


def _cells_at_distance(station_pos: Pos, radius: int):
    footprint = station_footprint(station_pos)
    xs = [p.x for p in footprint]
    ys = [p.y for p in footprint]
    cells = []
    for x in range(min(xs) - radius, max(xs) + radius + 1):
        for y in range(min(ys) - radius, max(ys) + radius + 1):
            pos = Pos(x, y)
            if pos in footprint:
                continue
            if footprint_distance(pos, footprint) == radius:
                cells.append(pos)
    return tuple(cells)


def tower_sites(state: "MatchState") -> tuple:
    """Demo：基地脚印距离为1的空地中取3格作为固定炮台位。"""
    station = own_station(state)
    if station is None or not state.map_info:
        return ()
    footprint = station_footprint(station.pos)
    cells = [pos for pos in _cells_at_distance(station.pos, 1) if is_land_cell(state, pos)]
    cells.sort(key=lambda pos: (footprint_distance(pos, footprint), pos.x, pos.y))
    return tuple(cells[:3])


def wall_order(state: "MatchState") -> tuple:
    """Demo：绕基地脚印一圈、外扩2格的围墙顺序，留一个入口。"""
    station = own_station(state)
    if station is None or not state.map_info:
        return ()
    footprint = station_footprint(station.pos)
    xs = [p.x for p in footprint]
    ys = [p.y for p in footprint]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    order = [
        *(Pos(x, ymin - 2) for x in range(xmax + 2, xmin - 3, -1)),
        *(Pos(xmin - 2, y) for y in range(ymin - 1, ymax + 2)),
        *(Pos(x, ymax + 2) for x in range(xmin - 2, xmax + 3)),
        *(Pos(xmax + 2, y) for y in range(ymax + 1, ymin - 2, -1)),
    ]
    entrance = Pos(xmax + 2, ymin - 1)
    return tuple(pos for pos in order if pos != entrance and is_land_cell(state, pos))


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
    """用上一回合的执行结果反馈修正建造黑名单（固定蓝图失败格跳过）。"""
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


def _pos_key(pos: Pos):
    return (pos.x, pos.y)


def _build_or_walk(worker: Role, target: Pos, name: str, blocked: set, reserved: set, width: int, height: int):
    """Demo：已在目标一格内则 build，否则走向目标旁。"""
    if worker.pos != target and chebyshev(worker.pos, target) <= 1:
        reserved.add(_pos_key(target))
        return {"action": "build", "name": name, "targetPos": [{"x": target.x, "y": target.y}]}
    step = move_towards(worker.pos, target, blocked | reserved, width, height)
    if step:
        reserved.add((step.x, step.y))
        return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def _adjacent_stone_mine(state: "MatchState", worker: Role):
    mines = []
    for zone in state.map_info.zones:
        if zone.neutral_type != WALL_MATERIAL:
            continue
        if worker.pos != zone.pos and chebyshev(worker.pos, zone.pos) <= 1:
            mines.append(zone.pos)
    if not mines:
        return None
    return min(mines, key=lambda pos: (chebyshev(worker.pos, pos), pos.x, pos.y))


def _mine_stone(worker: Role, state: "MatchState", blocked: set, reserved: set):
    if len(worker.backpack) >= worker.back_pack_capability:
        return None
    width, height = state.map_info.width, state.map_info.height
    mines = sorted(
        (
            z.pos for z in state.map_info.zones
            if z.neutral_type == WALL_MATERIAL and _pos_key(z.pos) not in reserved
        ),
        key=lambda pos: (chebyshev(worker.pos, pos), pos.x, pos.y),
    )
    for mine in mines:
        if worker.pos != mine and chebyshev(worker.pos, mine) <= 1:
            reserved.add(_pos_key(mine))
            return {"action": "collect", "targetPos": [{"x": mine.x, "y": mine.y}]}
        step = move_towards(worker.pos, mine, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def decide_worker_day(
    worker: Role,
    state: "MatchState",
    blocked: set,
    reserved: set,
    sites: tuple,
    free_towers: list,
    free_walls: list,
):
    """Demo 建造动线：先补齐固定炮台位，再攒石建围墙蓝图；完成后才做卖矿/升级。"""
    heal_cmd = decide_self_heal(worker)
    if heal_cmd:
        return heal_cmd

    width, height = state.map_info.width, state.map_info.height

    if free_towers and state.team_our.gold_num >= WEAPON_GOLD_COST:
        for index, site in enumerate(sites):
            if site in free_towers and _pos_key(site) not in reserved and _pos_key(site) not in state.failed_build_spots:
                name = TOWER_LOADOUT[index] if index < len(TOWER_LOADOUT) else pick_weapon_name(state)
                cmd = _build_or_walk(worker, site, name, blocked, reserved, width, height)
                if cmd:
                    if cmd["action"] == "build":
                        free_towers.remove(site)
                    return cmd

    if free_walls:
        stones = worker.backpack.count(WALL_MATERIAL)
        mine = _adjacent_stone_mine(state, worker)
        if mine is not None and stones < STONE_BATCH:
            reserved.add(_pos_key(mine))
            return {"action": "collect", "targetPos": [{"x": mine.x, "y": mine.y}]}
        if stones:
            for site in list(free_walls):
                if _pos_key(site) in reserved or _pos_key(site) in state.failed_build_spots:
                    continue
                cmd = _build_or_walk(worker, site, "wall", blocked, reserved, width, height)
                if cmd:
                    if cmd["action"] == "build":
                        free_walls.remove(site)
                    return cmd
            return None
        mine_cmd = _mine_stone(worker, state, blocked, reserved)
        if mine_cmd:
            return mine_cmd

    vendor = find_zone(state, "vendor")
    ore_in_backpack = [item for item in worker.backpack if item in ORE_TYPES]
    if vendor and ore_in_backpack and chebyshev(worker.pos, vendor.pos) <= 1:
        name, num = Counter(ore_in_backpack).most_common(1)[0]
        return {"action": "sell", "name": name, "num": num}

    buy_cmd = decide_buy_medicine(worker, state)
    if buy_cmd:
        return buy_cmd

    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    if vendor and len(ore_in_backpack) >= int(worker.back_pack_capability * BACKPACK_SELL_RATIO):
        step = move_towards(worker.pos, vendor.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
        return None

    maybe_start_shop_item_job(worker, state)
    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    mine = nearest_mine(state, worker)
    if mine:
        if chebyshev(worker.pos, mine.pos) <= 1:
            return {"action": "collect", "targetPos": [{"x": mine.pos.x, "y": mine.pos.y}]}
        step = move_towards(worker.pos, mine.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return None


def decide_pioneer_day(pioneer: Role, state: "MatchState", blocked: set, reserved: set, sites: tuple):
    """开拓者白天：自疗/买药/升级；若有炮台则贴过去待命（Demo 风格）。"""
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
    item_job_cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES]
    if weapons:
        nearest = min(weapons, key=lambda w: chebyshev(pioneer.pos, w.pos))
        if chebyshev(pioneer.pos, nearest.pos) > 1:
            step = move_towards(
                pioneer.pos, nearest.pos, blocked | reserved,
                state.map_info.width, state.map_info.height,
            )
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
    sites = tower_sites(state)
    order = wall_order(state)
    standing_towers = {_pos_key(r.pos) for r in state.team_our.roles if r.role_type in WEAPON_TYPES}
    standing_walls = {_pos_key(r.pos) for r in state.team_our.roles if r.role_type == "wall"}
    free_towers = [
        pos for pos in sites
        if _pos_key(pos) not in standing_towers and _pos_key(pos) not in blocked
    ]
    free_walls = [
        pos for pos in order
        if _pos_key(pos) not in standing_walls and _pos_key(pos) not in blocked
    ]
    for role in state.team_our.roles:
        if role.role_type == "worker":
            cmd = decide_worker_day(
                role, state, blocked, reserved, sites, free_towers, free_walls,
            )
        elif role.role_type == "pioneer":
            cmd = decide_pioneer_day(role, state, blocked, reserved, sites)
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
