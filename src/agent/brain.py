"""V1策略实现：白天经济循环+夜晚武器操控战斗。"""
from collections import Counter
import logging
from copy import copy
from typing import Optional

from .protocol import (
    ActionValidator, GameState, MatchState, Pos, Role, Strategy
)
from .decision_log import trace, selected
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
BUILD_RETRY_ROUNDS = 30  # 经验性重试间隔，不是官方规则。

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


def traced_move(state, role_id, start, target, blocked, width, height):
    step = move_towards(start, target, blocked, width, height)
    code = "path_found" if step else ("already_adjacent" if chebyshev(start, target) <= 1 else "unreachable")
    message = {"path_found": "前往目标的交互邻接格", "already_adjacent": "已在目标一格内，无需移动",
               "unreachable": "在当前障碍物和本回合预留格约束下，目标邻接格不可达"}[code]
    trace(state, role_id, code, message, target={"x": target.x, "y": target.y})
    return step


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


def pick_build_target(state: "MatchState", base_pos: Pos, blocked: set, kind: str = "weapon") -> Optional[Pos]:
    """在基地周围环形扩展搜索一个未阻挡、未被记录为建造失败的候选格。"""
    width, height = state.map_info.width, state.map_info.height
    if kind == "wall":
        from .opening import wall_ring
        base = own_station(state)
        if base is None:
            return None
        return next((Pos(x, y) for x, y in wall_ring(state, base)
                     if (x, y) not in blocked and (x, y, kind) not in state.failed_build_spots), None)
    for dx, dy in _BUILD_RING_OFFSETS:
        x, y = base_pos.x + dx, base_pos.y + dy
        if not (0 <= x < width and 0 <= y < height):
            continue
        key = (x, y)
        if (x, y, kind) in state.failed_build_spots or key in blocked:
            continue
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
    if state.team_our.gold_num < item_cost("Medicine", state):
        return None
    if len(role.backpack) >= role.back_pack_capability:
        return None
    return selected(state, role.id, {"action": "buy", "name": "Medicine", "num": 1}, '路过商店，金币与背包空间满足，补充药品')



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
    trace(state, role.id, "shop_job_check", "检查维修与升级任务（预算为本回合尚未分配余额）",
          available_gold=state.team_our.gold_num, reserved_target_count=len(pending_targets))

    damaged_wall = _pick_damaged_wall(state, pending_targets)
    if damaged_wall and state.team_our.gold_num >= WALL_FIXER_GOLD_COST:
        state.worker_item_jobs[role.id] = {
            "item": "WallFixer", "target": (damaged_wall.pos.x, damaged_wall.pos.y), "kind": "wall",
        }
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
            return

    station = own_station(state)
    if station and (station.level or 1) < 3 and (station.pos.x, station.pos.y) not in pending_targets:
        name, cost = voucher_for("station", station.level or 1)
        if state.team_our.gold_num >= cost:
            state.worker_item_jobs[role.id] = {"item": name, "target": (station.pos.x, station.pos.y), "kind": "station"}
            return
        trace(state, role.id, "station_upgrade_unaffordable", "基地可升级，但余额不足", available_gold=state.team_our.gold_num, required_gold=cost)



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
        trace(state, role.id, "job_target_missing", "道具任务目标建筑已不存在，释放任务")
        del state.worker_item_jobs[role.id]
        return None

    # 旧存档中尚未购买的基地升级任务，让位于武器/城墙。
    if job.get("kind") == "station" and job["item"] not in role.backpack:
        available = _pending_item_job_targets(state) - {tuple(job["target"])}
        defense = _pick_upgradeable(state, WEAPON_TYPES + ("wall",), available)
        if defense:
            del state.worker_item_jobs[role.id]
            maybe_start_shop_item_job(role, state)
            job = state.worker_item_jobs.get(role.id)
            if not job:
                return None
    width, height = state.map_info.width, state.map_info.height
    item = job["item"]
    x, y = job["target"]
    target = Pos(x, y)

    if job.get("awaiting_use"):
        previous = state.last_sent_command.get(role.id, {})
        confirmed = (previous.get("action") == "use" and previous.get("name") == item
                     and state.last_round_role_action_results.get(role.id) is True)
        if confirmed or item not in role.backpack:
            del state.worker_item_jobs[role.id]
            return None
        job.pop("awaiting_use", None)

    if item in role.backpack:
        if chebyshev(role.pos, target) <= 1:
            job["awaiting_use"] = True
            return selected(state, role.id, {"action": "use", "name": item, "targetPos": [{"x": x, "y": y}]}, '执行维修/升级道具任务')
        step = traced_move(state, role.id, role.pos, target, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return selected(state, role.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行维修/升级道具任务')
        return None

    if state.team_our.gold_num < item_cost(item, state):
        trace(state, role.id, "insufficient_gold", "道具任务购买资金不足，释放任务", available_gold=state.team_our.gold_num, required_gold=item_cost(item, state), item=item)
        del state.worker_item_jobs[role.id]
        return None
    shop = find_zone(state, "weaponShop")
    if shop is None:
        trace(state, role.id, "shop_missing", "快照中没有武器商店，释放道具任务")
        del state.worker_item_jobs[role.id]
        return None
    if chebyshev(role.pos, shop.pos) <= 1:
        if len(role.backpack) >= role.back_pack_capability:
            trace(state, role.id, "backpack_full", "背包已满，无法购买道具")
            del state.worker_item_jobs[role.id]
            return None
        return selected(state, role.id, {"action": "buy", "name": item, "num": 1}, '执行维修/升级道具任务')
    step = traced_move(state, role.id, role.pos, shop.pos, blocked | reserved, width, height)
    if step:
        reserved.add((step.x, step.y))
        return selected(state, role.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行维修/升级道具任务')
    return None



def item_cost(name: str, state: "MatchState") -> int:
    for item in state.weapon_shop_list:
        if item.name == name:
            return item.price
    if name == "Medicine":
        return MEDICINE_GOLD_COST
    if name == "WallFixer":
        return WALL_FIXER_GOLD_COST
    for kind in ("weapon", "station", "wall"):
        for level in (1, 2):
            item, cost = voucher_for(kind, level)
            if item == name:
                return cost
    return 0


def learn_from_last_round(state: "MatchState") -> None:
    """失败只代表暂时不可用；按建筑类型冷却30回合，不能推断永久非法。"""
    now = state.round_no or 0
    state.build_retry_after = {k: v for k, v in state.build_retry_after.items() if v > now}
    state.failed_build_spots = set(state.build_retry_after)
    for role_id, success in state.last_round_role_action_results.items():
        prev = state.last_sent_command.get(role_id, {})
        if success or prev.get("action") != "build":
            continue
        kind = "wall" if prev.get("name") == "wall" else "weapon"
        for pos in prev.get("targetPos", []):
            key = (pos["x"], pos["y"], kind)
            state.build_retry_after[key] = now + BUILD_RETRY_ROUNDS
            state.failed_build_spots.add(key)


def try_build(worker: Role, state: "MatchState", blocked: set, reserved: set):
    """机会性建造：优先补齐3座武器位，其次消耗背包里的石头建围墙。"""
    base = own_station(state)
    if base is None or state.map_info is None:
        return None

    pending = state.worker_build_targets.get(worker.id)
    if pending and pending[2] == "wall":
        from .opening import wall_ring
        if pending[:2] not in wall_ring(state, base):
            del state.worker_build_targets[worker.id]
            pending = None
    if pending:
        x, y, kind = pending
        if ((x, y, kind) in state.failed_build_spots or (x, y) in blocked or (x, y) in reserved
                or (kind == "weapon" and sum(r.role_type in WEAPON_TYPES for r in state.team_our.roles)
                    + getattr(state, "planned_weapons", 0) >= MAX_WEAPONS)):
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
                reserved.add((x, y))
                return selected(state, worker.id, {"action": "build", "name": name, "targetPos": [{"x": x, "y": y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
            step = traced_move(state, worker.id, worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
            if step:
                reserved.add((step.x, step.y))
                return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
            return None

    weapon_count = sum(1 for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    can_weapon = state.team_our.gold_num >= WEAPON_GOLD_COST and weapon_count + getattr(state, "planned_weapons", 0) < MAX_WEAPONS
    can_wall = "stone" in worker.backpack
    trace(state, worker.id, "build_conditions", "本回合建造条件；满足武器条件时优先武器", available_gold=state.team_our.gold_num, weapon_count=weapon_count, planned_weapons=getattr(state, "planned_weapons", 0), can_weapon=can_weapon, stone_count=worker.backpack.count("stone"), can_wall=can_wall)
    if can_weapon:
        kind = "weapon"
    elif can_wall:
        kind = "wall"
    else:
        return None

    pending_spots = {(x, y) for x, y, _ in state.worker_build_targets.values()}
    target = pick_build_target(state, base.pos, blocked | reserved | pending_spots, kind)
    if target is None:
        trace(state, worker.id, "no_build_candidate", "搜索范围内无可用建造候选格（占用、越界或失败冷却）", kind=kind)
        return None
    state.worker_build_targets[worker.id] = (target.x, target.y, kind)
    reserved.add((target.x, target.y))
    step = traced_move(state, worker.id, worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
    if step:
        reserved.add((step.x, step.y))
        return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
    return None



def decide_worker_day(worker: Role, state: "MatchState", blocked: set, reserved: set):
    heal_cmd = decide_self_heal(worker)
    if heal_cmd:
        return selected(state, worker.id, heal_cmd, "低血量且持有药品，治疗优先")

    width, height = state.map_info.width, state.map_info.height
    vendor = find_zone(state, "vendor")
    ore_in_backpack = [item for item in worker.backpack if item in ORE_TYPES]

    if vendor and ore_in_backpack and chebyshev(worker.pos, vendor.pos) <= 1:
        name, num = Counter(ore_in_backpack).most_common(1)[0]
        return selected(state, worker.id, {"action": "sell", "name": name, "num": num}, "已在小贩一格内，优先出售数量最多的矿石")

    buy_cmd = decide_buy_medicine(worker, state)
    if buy_cmd:
        return buy_cmd

    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    if vendor and len(ore_in_backpack) >= int(worker.back_pack_capability * BACKPACK_SELL_RATIO):
        trace(state, worker.id, "sell_threshold", "矿石数量达到出售阈值，前往小贩", ore_count=len(ore_in_backpack))
        step = traced_move(state, worker.id, worker.pos, vendor.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行经济循环：就近卖矿、达到阈值返售或采集最近矿点')
        return None

    build_cmd = try_build(worker, state, blocked, reserved)
    if build_cmd:
        return build_cmd

    maybe_start_shop_item_job(worker, state)
    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    mine = nearest_mine(state, worker)
    if mine is None:
        trace(state, worker.id, "no_mine", "当前快照没有石、铁、铜矿点")
    if mine:
        trace(state, worker.id, "mine_selected", "按切比雪夫距离选择最近矿点", mineral=mine.neutral_type, target={"x": mine.pos.x, "y": mine.pos.y})
        if chebyshev(worker.pos, mine.pos) <= 1:
            return selected(state, worker.id, {"action": "collect", "targetPos": [{"x": mine.pos.x, "y": mine.pos.y}]}, "已在最近矿点一格内，执行采集")
        step = traced_move(state, worker.id, worker.pos, mine.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行经济循环：就近卖矿、达到阈值返售或采集最近矿点')
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
    """先锋优先接取和保持任务，其余时间补给与升级。"""
    handled, command = decide_pioneer_task(pioneer, state, blocked, reserved)
    if handled:
        return command
    heal_cmd = decide_self_heal(pioneer)
    if heal_cmd:
        return selected(state, pioneer.id, heal_cmd, "低血量且持有药品，治疗优先")

    buy_cmd = decide_buy_medicine(pioneer, state)
    if buy_cmd:
        return buy_cmd

    item_job_cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    maybe_start_shop_item_job(pioneer, state)
    cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if cmd is None:
        trace(state, pioneer.id, "no_pioneer_action", "当前没有可用任务，也未产生治疗、补给或维修升级动作", available_gold=state.team_our.gold_num)
    return cmd


def plan_day(state: "MatchState") -> dict:
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    # 仅复制本回合预算；任务字典仍与真实状态共享，保留跨回合计划。
    state = copy(state)
    state.team_our = copy(state.team_our)
    state.planned_weapons = 0
    blocked = build_blocked_set(state)
    reserved = set()
    for role in state.team_our.roles:
        if role.role_type == "worker":
            cmd = decide_worker_day(role, state, blocked, reserved)
        elif role.role_type == "pioneer":
            cmd = decide_pioneer_day(role, state, blocked, reserved)
        else:
            continue
        if cmd:
            cost = 0
            if cmd["action"] == "buy":
                cost = item_cost(cmd["name"], state) * cmd.get("num", 1)
            elif cmd["action"] == "build" and cmd["name"] in WEAPON_TYPES:
                cost = WEAPON_GOLD_COST
                state.planned_weapons += 1
            if cost > state.team_our.gold_num:
                trace(state, role.id, "budget_rejected", "本回合剩余预算不足，取消指令", required_gold=cost, available_gold=state.team_our.gold_num)
                continue
            state.team_our.gold_num -= cost
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


def plan_pioneer_tasks(state, blocked, reserved):
    """先规划先锋任务；接管的先锋不再参与开局或武器分配。"""
    commands, handled_ids = {}, set()
    for role in state.team_our.roles:
        if role.role_type != "pioneer":
            continue
        handled, command = decide_pioneer_task(role, state, blocked, reserved)
        if handled:
            handled_ids.add(role.id)
            trace(state, role.id, "pioneer_task", "先锋任务优先，保留接取、原地解题和治疗逻辑")
            if command:
                commands[role.id] = command
    return commands, handled_ids


def plan_night(state: "MatchState") -> dict:
    from .opening import assign_weapons, adjacent_path, move_on_path
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    blocked, reserved = build_blocked_set(state), set()
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    assignments = assign_weapons(state, excluded_ids=task_pioneers)
    robots = state.robot.roles if state.robot else []
    for fighter in sorted(state.team_our.roles, key=lambda r: r.id):
        if fighter.role_type not in ("worker", "pioneer"):
            continue
        if fighter.id in task_pioneers:
            continue
        heal = decide_self_heal(fighter)
        if heal:
            commands[fighter.id] = selected(state, fighter.id, heal, "低血量优先自救")
            continue
        weapon = assignments.get(fighter.id)
        if weapon is None:
            trace(state, fighter.id, "no_free_weapon", "没有可分配的独立武器")
            continue
        trace(state, fighter.id, "weapon_assignment", "一人一炮；冷却和移动期间也保留分配", weapon_id=weapon.id)
        if chebyshev(fighter.pos, weapon.pos) <= 1:
            ready = weapon.role_type != "rocket" or (weapon.cooldown or 0) == 0
            target = pick_attack_target(weapon, robots) if ready else None
            if target:
                trace(state, fighter.id, "selected", "优先BOSS、大型、中型、小型；同等级优先低血量", weapon_id=weapon.id, target_robot_id=target.id)
                commands[weapon.id] = {"action": "attack", "controllerId": str(fighter.id),
                                       "targetPos": target_positions_for_weapon(weapon, target)}
            else:
                trace(state, fighter.id, "weapon_cooldown" if not ready else "no_target_in_range",
                      "火箭冷却，原地守炮" if not ready else "射程内无目标，原地守炮", weapon_id=weapon.id)
        else:
            cmd = move_on_path(state, fighter, adjacent_path(fighter, weapon.pos, blocked | reserved, state), reserved, "前往独立分配的武器")
            if cmd:
                commands[fighter.id] = cmd
    return commands



class BasicActionValidator(ActionValidator):
    """本地二次校验：只拦截能够确定的字段缺失/明显非法组合。"""

    _REQUIRES_TARGET_POS = ("move", "build", "remove", "collect")

    def validate(self, command: dict, state: GameState) -> None:
        action = command.get("action")
        allowed = {"move", "build", "remove", "collect", "attack", "sell", "buy",
                   "use", "drop", "acceptTask", "submitAnswer", "summonTreasure"}
        if action not in allowed:
            raise ValueError("unknown or missing action")
        positions = command.get("targetPos")
        if positions is not None:
            if not isinstance(positions, list) or not positions:
                raise ValueError("targetPos must be a nonempty list")
            if action != "attack" and len(positions) != 1:
                raise ValueError("action requires exactly one target")
            for pos in positions:
                if not isinstance(pos, dict) or any(type(pos.get(k)) is not int for k in ("x", "y")):
                    raise ValueError("target coordinates must be integers")
                if state is not None and state.map_info and not (0 <= pos["x"] < state.map_info.width and 0 <= pos["y"] < state.map_info.height):
                    raise ValueError("target outside map")
        if "num" in command and (type(command["num"]) is not int or command["num"] <= 0):
            raise ValueError("num must be a positive integer")
        if action == "build" and command.get("name") not in (*WEAPON_TYPES, "wall"):
            raise ValueError("unknown or missing building name")
        targeted_items = {"WallFixer", "DizzyWeapon", "Bomb"}
        name = command.get("name", "")
        if action == "use" and (name in targeted_items or "UpgradeVoucher" in name) and not positions:
            raise ValueError("targeted item requires targetPos")
        if action in self._REQUIRES_TARGET_POS and not command.get("targetPos"):
            raise ValueError(f"{action} requires targetPos")
        if action == "attack" and (not command.get("targetPos") or not command.get("controllerId")):
            raise ValueError("attack requires targetPos and controllerId")
        if action == "summonTreasure" and (not command.get("targetPos") or not command.get("item")):
            raise ValueError("summonTreasure requires targetPos and item")
        if action == "submitAnswer" and not command.get("taskAnswer"):
            raise ValueError("submitAnswer requires taskAnswer")
        if action in ("sell", "buy", "use", "drop") and not command.get("name"):
            raise ValueError(f"{action} requires name")


class V1Strategy(Strategy):
    """V1：白天经济+机会性建造，夜晚武器操控战斗。"""

    def __init__(self, validator: ActionValidator):
        self.validator = validator

    def decide(self, state: "MatchState") -> dict:
        state.decision_events = []
        learn_from_last_round(state)
        if not state.team_our or not state.map_info:
            trace(state, None, "missing_state", "缺少队伍或地图快照，不能生成指令")
            commands = {}
        elif isinstance(state.round_no, int) and 0 <= state.round_no < DAY_ROUNDS and own_station(state):
            from .opening import plan_opening
            commands = plan_opening(state)
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
            except ValueError as exc:
                trace(state, next((r.id for r in state.team_our.roles if str(r.id) == str(command.get("controllerId"))), role_id) if state.team_our else role_id, "validation_rejected", "本地指令校验失败", error=str(exc), command=command)
                logging.getLogger(__name__).warning("Dropped command for %s: %s (%r)", role_id, exc, command)
                continue
            valid[role_id] = command
        return valid
