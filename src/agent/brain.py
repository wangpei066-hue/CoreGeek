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
from .news_memory import vendor_prices


DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
DAY_NIGHT_CYCLE = DAY_ROUNDS + NIGHT_ROUNDS
WEAPON_TYPES = ("gatling", "railgun", "rocket")
# 用户确认编制：三座全火箭炮。升级优先迎敌最前、等级最低的那座。
WANTED_WEAPONS = ("rocket", "rocket", "rocket")
_WEAPON_UPGRADE_ORDER = {"rocket": 0, "railgun": 1, "gatling": 2}
MAX_WEAPONS = 3
WEAPON_GOLD_COST = 25
ORE_TYPES = ("stone", "iron", "copper")
BACKPACK_SELL_RATIO = 0.8
BUILD_RING_MIN_RADIUS = 2
BUILD_RING_MAX_RADIUS = 6
BUILD_RETRY_ROUNDS = 30  # 地形非法或多次不明失败后的长冷却，不是官方规则。
BUILD_RETRY_OCCUPIED = 2
BUILD_RETRY_UNKNOWN = 5
BUILD_RETRY_UNKNOWN_LIMIT = 3

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
EMERGENCY_HP_RATIO = 0.15
EMERGENCY_HP_ABS = 30
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


def nearest_mine(state: "MatchState", worker: Role, banned_ores=None, boosted_ores=None):
    """选择可采且价高优先的最近矿；banned_ores 来自新闻记忆的停工日程。"""
    if not state.map_info:
        return None
    banned = banned_ores or set()
    boosted = boosted_ores or set()
    prices = vendor_prices(state)
    candidates = [z for z in state.map_info.zones if z.neutral_type in ORE_TYPES and z.neutral_type not in banned]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda z: (
            0 if z.neutral_type in boosted else 1,
            -prices.get(z.neutral_type, 0),
            chebyshev(worker.pos, z.pos),
        ),
    )


def best_ore_to_sell(ore_in_backpack: list, state: "MatchState") -> tuple:
    """按小贩实价优先出售单价最高的矿种。"""
    prices = vendor_prices(state)
    counts = Counter(ore_in_backpack)
    name = max(counts.keys(), key=lambda n: (prices.get(n, 0), counts[n]))
    return name, counts[name]


def own_station(state: "MatchState"):
    if not state.team_our:
        return None
    return next((r for r in state.team_our.roles if r.role_type == "station"), None)


def pick_build_target(state: "MatchState", base_pos: Pos, blocked: set, kind: str = "weapon") -> Optional[Pos]:
    """在基地周围环形扩展搜索一个未阻挡、未被记录为建造失败的候选格。"""
    width, height = state.map_info.width, state.map_info.height
    if kind == "wall":
        from .opening import (
            staged_wall_plan, active_wall_plan, safe_wall, assign_weapons,
            full_wall_build_window, wall_priority,
        )
        base = own_station(state)
        if base is None:
            return None
        plan = staged_wall_plan(state, base)
        existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == "wall" and r.health > 0}
        if not set(plan) - existing:
            plan = active_wall_plan(state, base)
        if not full_wall_build_window(state):
            plan = [p for p in plan if wall_priority(state, base, p) == 0]
        return next((Pos(x, y) for x, y in plan
                     if (x, y) not in blocked and (x, y, kind) not in state.failed_build_spots
                     and safe_wall(state, (x, y), blocked, assign_weapons(state))), None)
    from .opening import weapon_candidates
    base = own_station(state)
    if base is None:
        return None
    for x, y in weapon_candidates(state, base, pick_weapon_name(state)):
        if not (0 <= x < width and 0 <= y < height):
            continue
        key = (x, y)
        if (x, y, kind) in state.failed_build_spots or key in blocked:
            continue
        return Pos(x, y)
    return None


def pick_weapon_name(state: "MatchState", extra_names=()) -> str:
    """按编制补齐火箭炮。extra_names计入本回合已规划建造。"""
    have = Counter(r.role_type for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    have.update(name for name in extra_names if name in WEAPON_TYPES)
    wanted = Counter(WANTED_WEAPONS)
    for name in WANTED_WEAPONS:
        if have[name] < wanted[name]:
            return name
    return "rocket"


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
    """半血以下用药；普通自救仍排在经济/战斗之后。"""
    if "Medicine" not in role.backpack:
        return None
    if role.health >= max_health(role) * HEAL_HP_RATIO:
        return None
    return {"action": "use", "name": "Medicine"}


def lethal_next_round(role: Role, state: "MatchState") -> bool:
    """近敌且血量可能被下一击打掉时视为紧急。机器人单次伤害未经官方表确认，阈值偏保守。"""
    if role.health <= 0:
        return False
    from .tactics import threat_robots
    nearby = [r for r in threat_robots(state) if chebyshev(role.pos, r.pos) <= 2]
    if not nearby:
        return False
    return role.health <= EMERGENCY_HP_ABS or role.health < max_health(role) * EMERGENCY_HP_RATIO


def decide_emergency_heal(role: Role, state: "MatchState"):
    """预计下一轮可能阵亡且有药时抢占当前动作。"""
    if "Medicine" not in role.backpack:
        return None
    if not lethal_next_round(role, state):
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


def _weapon_front_key(state: "MatchState", role: Role):
    """迎敌方向越靠前越小，供 min() 选取最前武器。"""
    from .opening import attack_direction
    base = own_station(state)
    if not base:
        return 0
    return -role.pos.x * attack_direction(state, base)


def _pick_upgradeable(state: "MatchState", role_types, pending_targets: set, min_health_ratio: float = 0.0,
                      max_current_level: int = 2):
    candidates = [
        r for r in state.team_our.roles
        if r.role_type in role_types
        and (r.pos.x, r.pos.y) not in pending_targets
        and (r.level or 1) <= max_current_level
        and r.health >= max_health(r) * min_health_ratio
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (
        r.level or 1,
        _weapon_front_key(state, r) if r.role_type in WEAPON_TYPES else 0,
        _WEAPON_UPGRADE_ORDER.get(r.role_type, 99),
        r.id,
    ))


def weapon_upgrade_due(state: "MatchState") -> bool:
    """日程上是否还该升武器；不看当前是否已有人锁定买券任务。"""
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    if not weapons:
        return False
    from .opening import DAY2_WALL_TARGET
    day = (state.round_no or 0) // DAY_NIGHT_CYCLE
    l2 = sum((w.level or 1) >= 2 for w in weapons)
    walls = sum(r.role_type == "wall" and r.health > 0 for r in state.team_our.roles)
    if day <= 0:
        return l2 < 1
    if day == 1:
        if l2 < 2:
            return True
        if walls < DAY2_WALL_TARGET:
            return False
        return any((w.level or 1) < 3 for w in weapons)
    return any((w.level or 1) < 3 for w in weapons)


def should_upgrade_weapon(state: "MatchState") -> bool:
    """按日程控制升级节奏：同一时刻只锁一门；首日只升最前一门，次日先再升一门并补墙，再依次升三级。"""
    if any(job.get("kind") == "weapon" for job in state.worker_item_jobs.values()):
        return False
    return weapon_upgrade_due(state)


def maybe_start_shop_item_job(role: Role, state: "MatchState", allow_weapon: bool = True,
                              allow_structure_upgrade: bool = True) -> None:
    """给空闲角色机会性分配一个"买道具->用道具"任务。"""
    if not state.team_our:
        return
    pending_targets = _pending_item_job_targets(state)
    trace(state, role.id, "shop_job_check", "检查维修与升级任务（预算为本回合尚未分配余额）",
          available_gold=state.team_our.gold_num, reserved_target_count=len(pending_targets))

    # 未购入任务按 升级武器 > 修墙 > 升墙/基地 让位。已买到手的道具继续用完。
    old_job = state.worker_item_jobs.get(role.id)
    item_name = old_job.get('item', '') if old_job else ''
    if old_job and item_name not in role.backpack:
        remaining = pending_targets - {tuple(old_job['target'])}
        weapon_due = _pick_upgradeable(state, WEAPON_TYPES, remaining, max_current_level=2)
        is_repair = item_name == 'WallFixer'
        wall_or_station_upgrade = (
            old_job.get('kind') == 'station'
            or (old_job.get('kind') == 'wall' and 'Upgrade' in item_name)
        )
        if is_repair and weapon_due and allow_weapon:
            trace(state, role.id, 'upgrade_job_preempted', '未购入的修墙任务让位于武器升级',
                  old_kind=old_job.get('kind'), weapon_id=weapon_due.id)
            del state.worker_item_jobs[role.id]
            pending_targets.discard(tuple(old_job['target']))
        elif wall_or_station_upgrade and weapon_due and allow_weapon:
            trace(state, role.id, 'upgrade_job_preempted', '未购入的低优先级升级任务让位于未满级武器',
                  old_kind=old_job.get('kind'), weapon_id=weapon_due.id)
            del state.worker_item_jobs[role.id]
            pending_targets.discard(tuple(old_job['target']))
        elif old_job.get('kind') == 'weapon' and weapon_due and allow_weapon:
            current = next((r for r in state.team_our.roles
                            if (r.pos.x, r.pos.y) == tuple(old_job['target']) and r.role_type in WEAPON_TYPES), None)
            if current and (
                (weapon_due.level or 1, _weapon_front_key(state, weapon_due), weapon_due.id)
                < (current.level or 1, _weapon_front_key(state, current), current.id)
            ):
                trace(state, role.id, 'upgrade_job_preempted', '未购入的武器升级让位于更靠前或更低级的火箭炮',
                      old_kind=current.role_type, weapon_id=weapon_due.id)
                del state.worker_item_jobs[role.id]
                pending_targets.discard(tuple(old_job['target']))
    if role.id in state.worker_item_jobs:
        return

    weapon = _pick_upgradeable(state, WEAPON_TYPES, pending_targets, max_current_level=2)
    if weapon and allow_weapon:
        name, cost = voucher_for("weapon", weapon.level or 1)
        if name in role.backpack:
            adjacent = [
                r for r in state.team_our.roles
                if r.role_type in WEAPON_TYPES
                and (r.pos.x, r.pos.y) not in pending_targets
                and (r.level or 1) <= 2
                and voucher_for("weapon", r.level or 1)[0] == name
                and chebyshev(role.pos, r.pos) <= 1
            ]
            if adjacent:
                weapon = min(adjacent, key=lambda r: (_weapon_front_key(state, r), r.level or 1, r.id))
        state.worker_item_jobs[role.id] = {"item": name, "target": (weapon.pos.x, weapon.pos.y), "kind": "weapon"}
        if name not in role.backpack and state.team_our.gold_num < item_cost(name, state):
            trace(state, role.id, 'weapon_upgrade_funding_gap', '已锁定武器升级目标，当前金币不足，禁止改做低优先级消费',
                  weapon_id=weapon.id, current_level=weapon.level or 1,
                  available_gold=state.team_our.gold_num, required_gold=item_cost(name, state),
                  next_level=(weapon.level or 1) + 1)
        return

    damaged_wall = _pick_damaged_wall(state, pending_targets)
    if damaged_wall and ('WallFixer' in role.backpack or state.team_our.gold_num >= item_cost('WallFixer', state)):
        state.worker_item_jobs[role.id] = {
            "item": "WallFixer", "target": (damaged_wall.pos.x, damaged_wall.pos.y), "kind": "wall",
        }
        return

    wall = _pick_upgradeable(state, ("wall",), pending_targets, min_health_ratio=WALL_REPAIR_RATIO)
    if wall and allow_structure_upgrade:
        name, cost = voucher_for("wall", wall.level or 1)
        if name in role.backpack or state.team_our.gold_num >= item_cost(name, state):
            state.worker_item_jobs[role.id] = {"item": name, "target": (wall.pos.x, wall.pos.y), "kind": "wall"}
            return

    station = own_station(state)
    if (allow_structure_upgrade and station and (station.level or 1) < 3
            and (station.pos.x, station.pos.y) not in pending_targets):
        name, cost = voucher_for("station", station.level or 1)
        if name in role.backpack or state.team_our.gold_num >= item_cost(name, state):
            state.worker_item_jobs[role.id] = {"item": name, "target": (station.pos.x, station.pos.y), "kind": "station"}
            return
        trace(state, role.id, "station_upgrade_unaffordable", "基地可升级，但余额不足", available_gold=state.team_our.gold_num, required_gold=cost)



def _job_target_still_exists(state: "MatchState", job: dict) -> bool:
    x, y = job["target"]
    role_types = _JOB_KIND_ROLE_TYPES.get(job.get("kind"), ())
    return any(r.pos.x == x and r.pos.y == y and r.role_type in role_types for r in state.team_our.roles)


def _droppable_for_purchase(role: Role):
    """买券前丢掉矿石腾空位，不丢升级券和药剂。"""
    for name in ("copper", "iron", "stone"):
        if name in role.backpack:
            return name
    return next((item for item in role.backpack
                 if isinstance(item, str) and "Voucher" not in item and item != "Medicine"), None)


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

    from .opening import adjacent_path, mobile_walkable, move_on_path
    walkable = mobile_walkable(state, blocked, reserved)
    if item in role.backpack:
        if chebyshev(role.pos, target) <= 1:
            job["awaiting_use"] = True
            return selected(state, role.id, {"action": "use", "name": item, "targetPos": [{"x": x, "y": y}]}, '执行维修/升级道具任务')
        return move_on_path(state, role, adjacent_path(role, target, walkable, state), reserved, '执行维修/升级道具任务')

    if state.team_our.gold_num < item_cost(item, state):
        trace(state, role.id, "insufficient_gold", "道具任务购买资金不足，释放任务", available_gold=state.team_our.gold_num, required_gold=item_cost(item, state), item=item)
        if job.get('kind') == 'weapon':
            trace(state, role.id, 'weapon_upgrade_job_waiting_funds', '保留武器升级目标并继续筹资，不改做城墙/基地升级')
            return None
        del state.worker_item_jobs[role.id]
        return None
    shop = find_zone(state, "weaponShop")
    if shop is None:
        trace(state, role.id, "shop_missing", "快照中没有武器商店，释放道具任务")
        del state.worker_item_jobs[role.id]
        return None
    if chebyshev(role.pos, shop.pos) <= 1:
        if len(role.backpack) >= role.back_pack_capability:
            drop = _droppable_for_purchase(role)
            if drop:
                trace(state, role.id, "backpack_full_drop", "背包已满，先丢掉矿石再购买升级券", drop=drop, item=item)
                return selected(state, role.id, {"action": "drop", "name": drop}, '腾出背包空位购买升级道具')
            trace(state, role.id, "backpack_full", "背包已满且没有可丢弃矿石，无法购买道具")
            return None
        return selected(state, role.id, {"action": "buy", "name": item, "num": 1}, '执行维修/升级道具任务')
    return move_on_path(state, role, adjacent_path(role, shop.pos, walkable, state), reserved, '执行维修/升级道具任务')



def item_cost(name: str, state: "MatchState") -> int:
    for item in state.weapon_shop_list:
        if item.name == name:
            return item.price
    if name == "Medicine":
        return MEDICINE_GOLD_COST
    if name == "WallFixer":
        return WALL_FIXER_GOLD_COST
    from .tactics import ITEM_COSTS
    if name in ITEM_COSTS:
        return ITEM_COSTS[name]
    for kind in ("weapon", "station", "wall"):
        for level in (1, 2):
            item, cost = voucher_for(kind, level)
            if item == name:
                return cost
    return 0


def _build_failure_reason(state: "MatchState", role_id, prev: dict) -> str:
    """根据占位、资源和错误描述分类建造失败；原因不明时只做短退避。"""
    targets = prev.get("targetPos") or []
    if not targets:
        return "unknown"
    pos = targets[0]
    cell = (pos["x"], pos["y"])
    occupied = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.health > 0}
    if cell in occupied:
        return "occupied"
    role = next((r for r in state.team_our.roles if r.id == role_id), None)
    if prev.get("name") == "wall" and role and "stone" not in role.backpack:
        return "resource"
    text = " ".join(e.description or "" for e in (state.errors or []))
    if any(token in text for token in ("非法", "不可建造", "禁止建造", "黄区", "蓝区", "超出建造")):
        return "illegal"
    return "unknown"


def learn_from_last_round(state: "MatchState") -> None:
    """建造失败按原因冷却：占位短退避，缺资源不封格，非法或多次不明失败才长冷却。"""
    now = state.round_no or 0
    state.build_retry_after = {k: v for k, v in state.build_retry_after.items() if v > now}
    state.failed_build_spots = set(state.build_retry_after)
    counts = dict(state.policy_memory.get("build_fail_counts") or {})
    for role_id, success in state.last_round_role_action_results.items():
        prev = state.last_sent_command.get(role_id, {})
        if success or prev.get("action") != "build":
            continue
        kind = "wall" if prev.get("name") == "wall" else "weapon"
        reason = _build_failure_reason(state, role_id, prev)
        for pos in prev.get("targetPos", []):
            key = (pos["x"], pos["y"], kind)
            stamp = f"{key[0]},{key[1]},{kind}"
            if reason == "resource":
                counts.pop(stamp, None)
                continue
            if reason == "occupied":
                delay, counts[stamp] = BUILD_RETRY_OCCUPIED, 0
            elif reason == "illegal":
                delay, counts[stamp] = BUILD_RETRY_ROUNDS, BUILD_RETRY_UNKNOWN_LIMIT
            else:
                counts[stamp] = int(counts.get(stamp, 0)) + 1
                delay = BUILD_RETRY_ROUNDS if counts[stamp] >= BUILD_RETRY_UNKNOWN_LIMIT else BUILD_RETRY_UNKNOWN
            state.build_retry_after[key] = now + delay
            state.failed_build_spots.add(key)
            trace(state, role_id, "build_retry", "建造失败已按原因退避",
                  reason=reason, cell=list(key), retry_after=state.build_retry_after[key])
    if counts:
        state.policy_memory["build_fail_counts"] = counts
    else:
        state.policy_memory.pop("build_fail_counts", None)


def try_build(worker: Role, state: "MatchState", blocked: set, reserved: set):
    """机会性建造：优先补齐3座武器位，其次消耗背包里的石头建围墙。仅工人可 build。"""
    if worker.role_type != "worker":
        return None
    base = own_station(state)
    if base is None or state.map_info is None:
        return None

    pending = state.worker_build_targets.get(worker.id)
    if pending and pending[2] == "wall":
        from .opening import active_wall_plan, full_wall_build_window, wall_priority
        if pending[:2] not in active_wall_plan(state, base):
            del state.worker_build_targets[worker.id]
            pending = None
        elif not full_wall_build_window(state, worker) and wall_priority(state, base, pending[:2]) != 0:
            del state.worker_build_targets[worker.id]
            pending = None
    if pending:
        x, y, kind = pending
        if ((x, y, kind) in state.failed_build_spots
                or ((x, y) in blocked and (x, y) != (worker.pos.x, worker.pos.y))
                or (x, y) in reserved
                or (kind == "weapon" and sum(r.role_type in WEAPON_TYPES for r in state.team_our.roles)
                    + getattr(state, "planned_weapons", 0) >= MAX_WEAPONS)):
            del state.worker_build_targets[worker.id]
        else:
            target = Pos(x, y)
            dist = chebyshev(worker.pos, target)
            if dist == 0:
                from .grid import neighbors8
                for step in neighbors8(worker.pos, state.map_info.width, state.map_info.height):
                    if (step.x, step.y) not in blocked | reserved:
                        reserved.add((step.x, step.y))
                        return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '先离开施工格再建造')
                return None
            if dist == 1:
                if kind == "wall":
                    from .opening import worker_should_build_walls
                    if not worker_should_build_walls(state, worker):
                        return None
                del state.worker_build_targets[worker.id]
                if kind == "weapon":
                    if state.team_our.gold_num < WEAPON_GOLD_COST:
                        return None
                    name = pick_weapon_name(state)
                else:
                    if "stone" not in worker.backpack:
                        return None
                    from .opening import safe_wall, assign_weapons
                    if not safe_wall(state, (x, y), blocked | reserved, assign_weapons(state)):
                        trace(state, worker.id, 'wall_route_blocked', '施工会截断通路，重新规划')
                        return None
                    name = "wall"
                reserved.add((x, y))
                return selected(state, worker.id, {"action": "build", "name": name, "targetPos": [{"x": x, "y": y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
            if kind == "wall":
                from .opening import wall_approach_path, move_on_path
                path = wall_approach_path(worker, target, blocked | reserved, state)
                return move_on_path(state, worker, path, reserved, '从院内接近城墙缺口')
            step = traced_move(state, worker.id, worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
            if step:
                reserved.add((step.x, step.y))
                return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
            return None

    weapon_count = sum(1 for r in state.team_our.roles if r.role_type in WEAPON_TYPES)
    can_weapon = state.team_our.gold_num >= WEAPON_GOLD_COST and weapon_count + getattr(state, "planned_weapons", 0) < MAX_WEAPONS
    from .opening import worker_should_build_walls
    can_wall = "stone" in worker.backpack and worker_should_build_walls(state, worker)
    trace(state, worker.id, "build_conditions", "本回合建造条件；满足武器条件时优先武器", available_gold=state.team_our.gold_num, weapon_count=weapon_count, planned_weapons=getattr(state, "planned_weapons", 0), can_weapon=can_weapon, stone_count=worker.backpack.count("stone"), can_wall=can_wall)
    if can_weapon:
        kind = "weapon"
    elif can_wall:
        kind = "wall"
    else:
        return None

    pending_spots = {(x, y) for x, y, _ in state.worker_build_targets.values()}
    own = {(worker.pos.x, worker.pos.y)}
    target = pick_build_target(state, base.pos, (blocked | reserved | pending_spots) - own, kind)
    if target is None:
        trace(state, worker.id, "no_build_candidate", "搜索范围内无可用建造候选格（占用、越界或失败冷却）", kind=kind)
        return None
    state.worker_build_targets[worker.id] = (target.x, target.y, kind)
    reserved.add((target.x, target.y))
    if kind == "wall":
        from .opening import wall_approach_path, move_on_path
        if chebyshev(worker.pos, target) == 1:
            del state.worker_build_targets[worker.id]
            if "stone" not in worker.backpack:
                return None
            from .opening import safe_wall, assign_weapons
            if not safe_wall(state, (target.x, target.y), blocked | reserved, assign_weapons(state)):
                return None
            return selected(state, worker.id, {"action": "build", "name": "wall", "targetPos": [{"x": target.x, "y": target.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
        path = wall_approach_path(worker, target, blocked | reserved, state)
        return move_on_path(state, worker, path, reserved, '从院内接近城墙缺口')
    step = traced_move(state, worker.id, worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
    if step:
        reserved.add((step.x, step.y))
        return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
    return None



def decide_pioneer_voucher(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """开拓者专责买/用武器升级券：任务金币够了就去商店，不走工人卖矿路径。进行中的任务不中断。"""
    if pioneer.role_type != "pioneer" or pioneer.health <= 0:
        return None
    if state.phase_task:
        return None
    job = state.worker_item_jobs.get(pioneer.id)
    has_voucher = any(isinstance(item, str) and "WeaponUpgradeVoucher" in item for item in pioneer.backpack)
    if job and job.get("kind") != "weapon" and not has_voucher:
        return None
    if has_voucher or (job and job.get("kind") == "weapon"):
        if has_voucher and (not job or job.get("kind") != "weapon"):
            maybe_start_shop_item_job(pioneer, state, allow_weapon=True)
        cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
        if cmd:
            trace(state, pioneer.id, "pioneer_voucher_job",
                  "开拓者执行武器升级券任务（购买或使用）", action=cmd.get("action"))
        return cmd
    from .economy import pick_weapon_voucher_buyer
    buyer = pick_weapon_voucher_buyer(state, blocked)
    if buyer is None or buyer.id != pioneer.id:
        return None
    if not should_upgrade_weapon(state):
        return None
    weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=2)
    if weapon is None:
        return None
    name, _ = voucher_for("weapon", weapon.level or 1)
    cost = item_cost(name, state)
    if state.team_our.gold_num < cost:
        trace(state, pioneer.id, "pioneer_voucher_wait_gold",
              "开拓者等任务金币凑够再买武器升级券", available_gold=state.team_our.gold_num, required_gold=cost)
        return None
    maybe_start_shop_item_job(pioneer, state, allow_weapon=True)
    cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if cmd:
        trace(state, pioneer.id, "pioneer_buys_voucher", "任务金币已够，开拓者去买武器升级券",
              item=name, available_gold=state.team_our.gold_num, required_gold=cost)
    return cmd


def decide_worker_day(worker: Role, state: "MatchState", blocked: set, reserved: set):
    from .economy import liquidate, profitable_mine, muster_for_night, worker_should_shop_weapon_voucher
    from .tactics import tactical_action
    from .opening import replenish_walls, staged_walls_incomplete, worker_should_build_walls
    heal = decide_emergency_heal(worker, state)
    if heal:
        return selected(state, worker.id, heal, '血量过低且近敌，紧急用药')
    handled, cmd = muster_for_night(worker, state, blocked, reserved)
    if handled:
        return cmd
    allow_build = worker_should_build_walls(state, worker)
    allow_weapon = worker_should_shop_weapon_voucher(worker, state, blocked)
    cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if cmd:
        return cmd
    if allow_weapon:
        maybe_start_shop_item_job(worker, state, allow_weapon=True)
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            return cmd
        handled, cmd = liquidate(worker, state, blocked, reserved)
        if cmd:
            return cmd
    handled, cmd = replenish_walls(worker, state, blocked, reserved, primary_only=True, allow_build=allow_build)
    if cmd:
        return cmd
    handled, cmd = liquidate(worker, state, blocked, reserved)
    if cmd:
        return cmd
    defer_upgrades = (weapon_upgrade_due(state)
                      or ((state.round_no or 0) >= 70 and staged_walls_incomplete(state) and not allow_build))
    maybe_start_shop_item_job(
        worker, state, allow_weapon=allow_weapon,
        allow_structure_upgrade=allow_weapon or not defer_upgrades,
    )
    cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if cmd:
        return cmd
    if sum(r.role_type in WEAPON_TYPES for r in state.team_our.roles) >= 3:
        handled, cmd = replenish_walls(worker, state, blocked, reserved, allow_build=allow_build)
        if cmd:
            return cmd
    cmd = tactical_action(worker, state, blocked, reserved)
    if cmd:
        return cmd

    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    build_cmd = try_build(worker, state, blocked, reserved)
    if build_cmd:
        return build_cmd

    return profitable_mine(worker, state, blocked, reserved) or decide_self_heal(worker) or decide_buy_medicine(worker, state)



def decide_pioneer_task(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """回防窗内是否留在任务点由 pioneer_should_hold_task 决定，不再用「三炮二级」一刀切。
    未开始的任务始终受 defense_due 约束，不会在回防期新接。"""
    if pioneer.health <= 0:
        return True, None
    from .economy import defense_due, pioneer_available_to_buy_voucher, pioneer_should_hold_task
    job = state.worker_item_jobs.get(pioneer.id)
    if job and job.get("kind") == "weapon" and not state.phase_task:
        return False, None
    if (not state.phase_task and pioneer_available_to_buy_voucher(state)
            and should_upgrade_weapon(state)):
        from .economy import pick_weapon_voucher_buyer
        buyer = pick_weapon_voucher_buyer(state, blocked)
        if buyer is not None and buyer.id == pioneer.id:
            weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=2)
            if weapon is not None:
                name, _ = voucher_for("weapon", weapon.level or 1)
                if state.team_our.gold_num >= item_cost(name, state) or any(
                        isinstance(item, str) and "WeaponUpgradeVoucher" in item for item in pioneer.backpack):
                    trace(state, pioneer.id, "task_yields_to_voucher",
                          "买券完整代价最低的是开拓者，先去商店，不新接任务")
                    return False, None
    if state.phase_task:
        if defense_due(pioneer, state, blocked) and not pioneer_should_hold_task(pioneer, state):
            trace(state, pioneer.id, 'task_yields_to_defense',
                  '无法在威胁到达前提交，或两门炮守不住当前波次，回炮；题目会话保留')
            return False, None
        return True, decide_emergency_heal(pioneer, state) or decide_self_heal(pioneer)
    memory = getattr(state, "news_memory", None)
    if memory is not None:
        from .treasure import decide_treasure_action, treasure_should_claim_pioneer
        if treasure_should_claim_pioneer(state, pioneer, memory):
            cmd = decide_treasure_action(pioneer, state, memory, blocked, reserved)
            return True, cmd
    if defense_due(pioneer, state, blocked):
        trace(state, pioneer.id, 'task_yields_to_defense', '回防时间已到或家中告急，不再新接任务')
        return False, None
    candidates = sorted(
        (t for t in state.team_our.player_tasks
         if t.task_type in ("自进化类1", "自进化类2")
         and t.is_valid and t.cold_down_rounds == 0),
        key=lambda t: (chebyshev(pioneer.pos, t.task_position), t.task_type),
    )
    if not candidates:
        if memory is not None:
            from .treasure import decide_treasure_action
            cmd = decide_treasure_action(pioneer, state, memory, blocked, reserved)
            if cmd:
                return True, cmd
        return False, None
    for task in candidates:
        from .opening import MUSTER_BUFFER, adjacent_path, assign_weapons
        from .tactics import night_wave_cleared, threat_eta_to_base
        route = adjacent_path(pioneer, task.task_position, blocked | reserved, state)
        weapon = assign_weapons(state).get(pioneer.id)
        return_distance = chebyshev(task.task_position, weapon.pos) if weapon else 8
        required = (len(route) if route is not None else 10000) + (task.timeout_rounds or 15) + return_distance + MUSTER_BUFFER
        arrival = threat_eta_to_base(state)
        if not night_wave_cleared(state) and (arrival is None or required >= arrival):
            trace(state, pioneer.id, 'task_not_enough_time', '任务行程、执行与回防余量不足，不再接取',
                  required_rounds=required, threat_eta=arrival)
            continue
        if chebyshev(pioneer.pos, task.task_position) <= 1:
            return True, {"action": "acceptTask"}
        step = move_towards(pioneer.pos, task.task_position, blocked | reserved,
                            state.map_info.width, state.map_info.height)
        if step:
            reserved.add((step.x, step.y))
            return True, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}
    return False, None


def decide_pioneer_day(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """先锋紧急自救、回防、买券优先，再执行白天任务与补给。"""
    from .economy import liquidate, muster_for_night
    from .tactics import tactical_action
    heal = decide_emergency_heal(pioneer, state)
    if heal:
        return selected(state, pioneer.id, heal, '血量过低且近敌，紧急用药')
    handled, cmd = muster_for_night(pioneer, state, blocked, reserved)
    if handled:
        return cmd
    cmd = decide_pioneer_voucher(pioneer, state, blocked, reserved)
    if cmd:
        return cmd
    handled, cmd = liquidate(pioneer, state, blocked, reserved)
    if handled:
        return cmd
    handled, command = decide_pioneer_task(pioneer, state, blocked, reserved)
    if handled:
        return command
    from .world_intel import decide_treasure
    handled, command = decide_treasure(pioneer, state, blocked, reserved)
    if handled:
        return command
    cmd = tactical_action(pioneer, state, blocked, reserved)
    if cmd:
        return cmd

    item_job_cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    maybe_start_shop_item_job(pioneer, state, allow_weapon=False)
    cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
    if cmd:
        return cmd
    heal = decide_self_heal(pioneer) or decide_buy_medicine(pioneer, state)
    if heal:
        return heal
    from .opening import pioneer_stay_clear
    cmd = pioneer_stay_clear(pioneer, state, blocked, reserved)
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
    from .opening import movement_avoid
    blocked = build_blocked_set(state) | movement_avoid(state)
    reserved = set()
    order = [r for r in state.team_our.roles if r.role_type == "pioneer"]
    order += [r for r in state.team_our.roles if r.role_type == "worker"]
    for role in order:
        if role.role_type == "worker":
            cmd = decide_worker_day(role, state, blocked, reserved)
        else:
            cmd = decide_pioneer_day(role, state, blocked, reserved)
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
            trace(state, role.id, "pioneer_task", "白天安全时间内执行任务，回防时让出角色")
            if command:
                commands[role.id] = command
    return commands, handled_ids


def plan_night(state: "MatchState") -> dict:
    from .opening import assign_weapons, move_on_path, movement_avoid, weapon_approach_path, _fighter_layer
    from .tactics import (
        tactical_action, night_wave_cleared, night_near_work_allowed,
        threat_robots, pressure, front_breached,
    )
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    state = copy(state)
    state.team_our = copy(state.team_our)
    blocked, reserved = build_blocked_set(state) | movement_avoid(state), set()
    robots = threat_robots(state)
    if night_wave_cleared(state):
        trace(state, None, 'night_wave_cleared', '夜间威胁已清空，转为任务、采矿和修墙抢回合')
        for role in state.team_our.roles:
            if role.role_type == 'worker':
                cmd = decide_worker_day(role, state, blocked, reserved)
            elif role.role_type == 'pioneer':
                cmd = decide_pioneer_day(role, state, blocked, reserved)
            else:
                continue
            if cmd:
                if cmd.get('action') == 'buy':
                    cost = item_cost(cmd['name'], state) * cmd.get('num', 1)
                    if cost > state.team_our.gold_num:
                        continue
                    state.team_our.gold_num -= cost
                commands[role.id] = cmd
        return commands
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    assignments = assign_weapons(state, excluded_ids=task_pioneers, persist=True)
    urgent = pressure(state) or front_breached(state)
    fighters = [r for r in state.team_our.roles
                if r.role_type in ("worker", "pioneer") and r.health > 0 and r.id not in task_pioneers]
    fighters.sort(key=lambda r: (
        0 if (assignments.get(r.id) and chebyshev(r.pos, assignments[r.id].pos) <= 1) else 1,
        _fighter_layer(state, r),
    ))
    for fighter in fighters:
        heal = decide_emergency_heal(fighter, state)
        if heal:
            commands[fighter.id] = selected(state, fighter.id, heal, '血量过低且近敌，紧急用药')
            continue
        if urgent:
            cmd = tactical_action(fighter, state, blocked, reserved, allow_travel=False)
            if cmd:
                if cmd['action'] == 'buy':
                    state.team_our.gold_num -= item_cost(cmd['name'], state)
                commands[fighter.id] = cmd
                continue
        weapon = assignments.get(fighter.id)
        if weapon is not None and chebyshev(fighter.pos, weapon.pos) <= 1:
            ready = weapon.role_type != "rocket" or (weapon.cooldown or 0) == 0
            target = pick_attack_target(weapon, robots) if ready else None
            if target:
                trace(state, fighter.id, "weapon_assignment", "一人一炮；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
                trace(state, fighter.id, "selected", "优先BOSS、大型、中型、小型；同等级优先低血量", weapon_id=weapon.id, target_robot_id=target.id)
                commands[weapon.id] = {"action": "attack", "controllerId": str(fighter.id),
                                       "targetPos": target_positions_for_weapon(weapon, target)}
                continue
            trace(state, fighter.id, "weapon_assignment", "一人一炮；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
            if fighter.role_type == 'worker' and night_near_work_allowed(state) and not robots:
                from .opening import adjacent_critical_build
                near = adjacent_critical_build(fighter, state, blocked, reserved)
                if near:
                    commands[fighter.id] = near
                    continue
                cmd = tactical_action(fighter, state, blocked, reserved, allow_travel=False)
                if cmd:
                    if cmd['action'] == 'buy':
                        state.team_our.gold_num -= item_cost(cmd['name'], state)
                    commands[fighter.id] = cmd
                    continue
            trace(state, fighter.id, "weapon_cooldown" if not ready else "no_target_in_range",
                  "火箭冷却，原地守炮" if not ready else "射程内无目标，原地守炮", weapon_id=weapon.id)
            continue
        if weapon is None:
            trace(state, fighter.id, "no_free_weapon", "没有可分配的独立武器")
            from .economy import muster_for_night
            _, cmd = muster_for_night(fighter, state, blocked, reserved)
            if cmd:
                commands[fighter.id] = cmd
            continue
        trace(state, fighter.id, "weapon_assignment", "一人一炮；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
        cmd = move_on_path(state, fighter, weapon_approach_path(fighter, weapon, blocked, reserved, state), reserved, "前往独立分配的武器")
        if cmd:
            commands[fighter.id] = cmd
    for fighter in state.team_our.roles:
        if fighter.id in task_pioneers:
            continue
        controlling = any(c.get('controllerId') == str(fighter.id) for c in commands.values())
        if fighter.role_type in ('worker', 'pioneer') and fighter.id not in commands and not controlling:
            heal = decide_self_heal(fighter)
            if heal:
                commands[fighter.id] = selected(state, fighter.id, heal, '没有防守动作可执行，最后自救')
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
        news_codes = {
            "ore_heuristic", "legend_appended", "ore_decoded", "treasure_decoded",
            "llm_request", "llm_empty", "llm_parse_failed", "summon_result",
        }
        kept = [e for e in (state.decision_events or []) if e.get("code") in news_codes]
        state.decision_events = kept
        from .tactics import begin_round
        begin_round(state)
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
        # 最低优先级兜底，不能同时占用正在操炮的角色。
        if state.team_our:
            controllers = {str(c.get('controllerId')) for c in commands.values()}
            for role in state.team_our.roles:
                if role.role_type in ('worker', 'pioneer') and role.health > 0 and role.id not in commands and str(role.id) not in controllers:
                    heal = decide_self_heal(role)
                    if heal:
                        commands[role.id] = selected(state, role.id, heal, '所有更高优先级分支均无行动，最后自救')
        commands = self._filter_valid(commands, state)
        state.last_sent_command = commands
        return commands

    def _filter_valid(self, commands: dict, state: "MatchState") -> dict:
        valid = {}
        roles = {r.id: r for r in (state.team_our.roles if state.team_our else [])}
        for role_id, command in commands.items():
            role = roles.get(role_id)
            action = command.get("action")
            if role and action in ("build", "remove", "collect") and role.role_type != "worker":
                trace(state, role_id, "role_action_forbidden",
                      "采集、建造、拆除仅工人可用，已丢弃开拓者非法指令", command=command)
                continue
            if role and action in ("acceptTask", "submitAnswer") and role.role_type != "pioneer":
                trace(state, role_id, "role_action_forbidden",
                      "接取与提交任务仅开拓者可用，已丢弃非法指令", command=command)
                continue
            try:
                self.validator.validate(command, state)
            except ValueError as exc:
                trace(state, next((r.id for r in state.team_our.roles if str(r.id) == str(command.get("controllerId"))), role_id) if state.team_our else role_id, "validation_rejected", "本地指令校验失败", error=str(exc), command=command)
                logging.getLogger(__name__).warning("Dropped command for %s: %s (%r)", role_id, exc, command)
                continue
            valid[role_id] = command
        return valid
