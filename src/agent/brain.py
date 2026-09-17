"""V1策略实现：白天经济循环+夜晚武器操控战斗。"""
from collections import Counter
import logging
from copy import copy
from typing import Optional

from .protocol import (
    ActionValidator, GameState, MatchState, Pos, Role, Strategy
)
from .decision_log import trace, selected, log_judge_feedback
from .grid import build_blocked_set, chebyshev, move_towards, nearest_adjacent_free_cell
from .news_memory import vendor_prices
from .targeting import DamageLedger, TargetContext, plan_attack
from .pioneer_schedule import (
    SCHEDULER_VERSION, SHOP_STALL_ROUNDS, add_branch, apply_task_choice, begin_schedule,
    bump_streak, emit_scheduler_log, ensure_schedule_buckets, interrupt_reservation,
    mark_outcome, reservation_of, reset_shop_progress, shop_progress_stalled,
    classify_shop_stall, has_task_reservation, voucher_is_defense_critical,
)


DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
DAY_NIGHT_CYCLE = DAY_ROUNDS + NIGHT_ROUNDS
WEAPON_TYPES = ("gatling", "railgun", "rocket")
# 用户确认编制：两门火箭炮 + 一门电磁炮；升级优先给火箭炮。
WANTED_WEAPONS = ("rocket", "rocket", "railgun")
_WEAPON_UPGRADE_ORDER = {"rocket": 0, "railgun": 1, "gatling": 2}
MAX_WEAPONS = 3
WALL_UPGRADE_HEALTH_RATIO = 0.5  # 围墙只在低于半血时才买券升级（升级回满血）。
WALL_UPGRADE_DUSK_WINDOW = 20  # 还有新墙要建时，离入夜这么多回合内才转去升级残墙。
NIGHT_REPAIR_GUNNER_TRAVEL = 1  # 有压力时放施工工修墙，守炮的两人最多离炮位这么多步。
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
WALL_REPAIR_RATIO = 0.8  # 「墙是否够健康」的判断阈值，不再用来派修墙包。
WALL_CRITICAL_RATIO = 0.15  # 没有近敌时，血量低于满血 15% 才视为即将摧毁。
WALL_CRITICAL_ABS = 80  # 约两次 BOSS 击或四次大型击；没有官方「下一击摧毁」表。
_ROBOT_ATTACK = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}
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
TARGETING_REASON = "按有效伤害×价值+击杀奖励选落点；火箭先算，电磁炮补刀"



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


def pick_build_target(state: "MatchState", base_pos: Pos, blocked: set, kind: str = "weapon",
                      worker: Optional[Role] = None) -> Optional[Pos]:
    """在基地周围环形扩展搜索一个未阻挡、未被记录为建造失败的候选格。
    墙：先正面后侧翼，同优先级选离施工工最近的格子，避免两端来回跑。"""
    width, height = state.map_info.width, state.map_info.height
    if kind == "wall":
        from .opening import (
            safe_wall, assign_weapons, wall_priority, due_wall_gaps,
        )
        base = own_station(state)
        if base is None:
            return None
        plan = due_wall_gaps(state, worker)
        existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == "wall" and r.health > 0}
        origin = worker.pos if worker is not None else base_pos
        from .opening import wall_approach_path
        ranked = []
        for p in plan:
            if p in blocked or (p[0], p[1], kind) in state.failed_build_spots:
                continue
            if not safe_wall(state, p, blocked, assign_weapons(state)):
                if worker is None or chebyshev(worker.pos, Pos(*p)) != 1:
                    continue
            path = None
            if worker is not None:
                path = wall_approach_path(worker, Pos(*p), blocked, state)
                if path is None:
                    continue
            adj_built = any(chebyshev(Pos(*p), Pos(*e)) == 1 for e in existing) if existing else True
            here = chebyshev(origin, Pos(*p))
            ranked.append((
                wall_priority(state, base, p),
                0 if here <= 1 else 1,
                0 if adj_built else 1,
                0 if path is None else len(path),
                here,
                p,
            ))
        ranked.sort()
        return next((Pos(x, y) for *_rest, (x, y) in ranked), None)
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
    """近敌且低血（≤30 或低于满血 15%）时视为紧急治疗。没有可靠伤害数据，不称为下一击致死。"""
    if role.health <= 0:
        return False
    from .tactics import threat_robots
    nearby = [r for r in threat_robots(state) if chebyshev(role.pos, r.pos) <= 2]
    if not nearby:
        return False
    return role.health <= EMERGENCY_HP_ABS or role.health < max_health(role) * EMERGENCY_HP_RATIO


def decide_emergency_heal(role: Role, state: "MatchState"):
    """低血紧急治疗：近敌且低于阈值、背包有药时抢占当前动作。"""
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
    if any(job.get('kind') == 'weapon' for job in state.worker_item_jobs.values()):
        return None
    if weapon_upgrade_due(state):
        voucher_cost = item_cost('WeaponUpgradeVoucher1', state)
        if state.team_our.gold_num < voucher_cost + item_cost('Medicine', state):
            return None
    return selected(state, role.id, {"action": "buy", "name": "Medicine", "num": 1}, '路过商店，金币与背包空间满足，补充药品')



def _pending_item_job_targets(state: "MatchState") -> set:
    return {tuple(job["target"]) for job in state.worker_item_jobs.values()}


def wall_about_to_fall(wall: Role, state: "MatchState") -> bool:
    """墙是否马上要被打掉。小型5/中型10/大型20/BOSS40、射程3来自任务书4.7.2；不是判题器实测。"""
    if wall is None or wall.role_type != 'wall' or wall.health <= 0:
        return False
    from .tactics import threat_robots
    nearby = [r for r in threat_robots(state) if chebyshev(wall.pos, r.pos) <= 3]
    if nearby:
        burst = sum(_ROBOT_ATTACK.get(r.role_type, 10) for r in nearby)
        return wall.health <= burst * 2
    return wall.health <= WALL_CRITICAL_ABS or wall.health < max_health(wall) * WALL_CRITICAL_RATIO


def _job_wall(state: "MatchState", job: dict):
    x, y = job.get('target', (None, None))
    return next((r for r in state.team_our.roles
                 if r.role_type == 'wall' and r.pos.x == x and r.pos.y == y), None)


def release_stale_repair_job(role: Role, state: "MatchState") -> None:
    """未买的修墙包、以及还没到即将摧毁的维修，都释放。升级会回满血，不要排队去商店买修复包。"""
    job = state.worker_item_jobs.get(role.id)
    if not job or job.get('item') != 'WallFixer':
        return
    wall = _job_wall(state, job)
    keep = 'WallFixer' in role.backpack and wall is not None and wall_about_to_fall(wall, state)
    if keep:
        return
    del state.worker_item_jobs[role.id]
    trace(state, role.id, 'repair_job_released',
          '未到即将摧毁不跑商店修墙；升级墙会回满血，优先新建和升级',
          wall_id=None if wall is None else wall.id,
          wall_health=None if wall is None else wall.health)


def _pick_damaged_wall(state: "MatchState", pending_targets: set):
    """只修即将摧毁且不能再升级的墙。能升级的墙用升级券回满血。"""
    candidates = [
        r for r in state.team_our.roles
        if r.role_type == "wall"
        and (r.pos.x, r.pos.y) not in pending_targets
        and wall_about_to_fall(r, state)
        and (r.level or 1) >= 3
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.health)


def _pick_front_wall_below_floor(state: "MatchState", pending_targets: set, floor: float = 0.5):
    """前排墙低于安全血线时优先升级；升级会回满血。"""
    base = own_station(state)
    if base is None:
        return None
    from .opening import primary_wall_plan, wall_priority
    front = {
        p for p in primary_wall_plan(state, base)
        if wall_priority(state, base, p) == 0
    }
    candidates = [
        r for r in state.team_our.roles
        if r.role_type == "wall"
        and r.health > 0
        and (r.pos.x, r.pos.y) in front
        and (r.pos.x, r.pos.y) not in pending_targets
        and r.health < max_health(r) * floor
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: ((r.level or 1) >= 3, r.health / max(1, max_health(r)), r.id))


def _weapon_front_key(state: "MatchState", role: Role):
    """迎敌方向越靠前越小，供 min() 选取最前武器。"""
    from .opening import attack_direction
    base = own_station(state)
    if not base:
        return 0
    return -role.pos.x * attack_direction(state, base)


def _pick_upgradeable(state: "MatchState", role_types, pending_targets: set, min_health_ratio: float = 0.0,
                      max_current_level: int = 2, below_health_ratio: Optional[float] = None):
    candidates = [
        r for r in state.team_our.roles
        if r.role_type in role_types
        and r.health > 0
        and (r.pos.x, r.pos.y) not in pending_targets
        and (r.level or 1) <= max_current_level
        and r.health >= max_health(r) * min_health_ratio
        and (below_health_ratio is None or r.health < max_health(r) * below_health_ratio)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (
        r.level or 1,
        _WEAPON_UPGRADE_ORDER.get(r.role_type, 99),
        _weapon_front_key(state, r),
        r.health,
        r.id,
    ))


def _ordered_rockets(state: "MatchState", pending_targets: set = ()):
    return sorted(
        (
            r for r in state.team_our.roles
            if r.role_type == "rocket" and r.health > 0
            and (r.pos.x, r.pos.y) not in pending_targets
        ),
        key=lambda r: (_weapon_front_key(state, r), r.id),
    )


def upgrade_plan(state: "MatchState"):
    """升级顺序全表，并把全队已买未用的券按顺序抵扣到对应步骤上。

    顺序：火箭A 1→2、火箭B 1→2、火箭A 2→3、基地 1→2、火箭B 2→3，其余武器（电磁炮等）1→2→3。
    每一步是 dict(role, level, kind, name, core, covered)：
    - covered：全队背包里已有这张券（已买未用），这一步不用再买，由持券人去用；
    - core：基地这一步及之前的步骤、以及所有 1 级升级，排在修墙前面。
    返回 (steps, surplus)：surplus 是按顺序抵扣后仍用不上的多余券数量。"""
    live = [r for r in state.team_our.roles if r.health > 0]
    rockets = _ordered_rockets(state)
    others = sorted((r for r in live if r.role_type in WEAPON_TYPES and r.role_type != "rocket"),
                    key=lambda r: (_WEAPON_UPGRADE_ORDER.get(r.role_type, 99), _weapon_front_key(state, r), r.id))
    station = own_station(state)
    first, second = (rockets + [None, None])[:2]
    sequence = []
    if first:
        sequence.append((first, 1))
    if second:
        sequence.append((second, 1))
    if first:
        sequence.append((first, 2))
    if station and second:
        sequence.append((station, 1))
    if second:
        sequence.append((second, 2))
    for weapon in rockets[2:] + others:
        sequence += [(weapon, 1), (weapon, 2)]
    held = Counter(item for r in live for item in (r.backpack or [])
                   if isinstance(item, str) and item.startswith(("WeaponUpgradeVoucher", "StationUpgradeVoucher")))
    steps = []
    core = True
    for role, level in sequence:
        if (role.level or 1) > level:
            continue
        kind = "station" if role.role_type == "station" else "weapon"
        name = voucher_for(kind, level)[0]
        covered = held[name] > 0
        if covered:
            held[name] -= 1
        steps.append(dict(role=role, level=level, kind=kind, name=name,
                          core=core or level == 1, covered=covered))
        if kind == "station":
            core = False
    return steps, held


def _step_key(step):
    return (step["role"].pos.x, step["role"].pos.y), step["name"]


def _claimed_upgrade_steps(state: "MatchState", exclude_role_id=None) -> Counter:
    """别人已认领、但还没买到券的武器/基地升级任务，按 (目标坐标, 券名) 计数。已买到的算在 covered 里。"""
    roles = {r.id: r for r in state.team_our.roles}
    claimed = Counter()
    for rid, job in state.worker_item_jobs.items():
        if rid == exclude_role_id or job.get("kind") not in ("weapon", "station"):
            continue
        holder = roles.get(rid)
        if holder is not None and job.get("item") in (holder.backpack or []):
            continue
        claimed[(tuple(job["target"]), job.get("item"))] += 1
    return claimed


def _open_upgrade_steps(state: "MatchState", exclude_role_id=None):
    """按顺序列出还需要有人去买的步骤：没被已买券抵扣、也没被别人认领。"""
    steps, _surplus = upgrade_plan(state)
    claimed = _claimed_upgrade_steps(state, exclude_role_id)
    open_steps = []
    for step in steps:
        if step["covered"]:
            continue
        key = _step_key(step)
        if claimed[key]:
            claimed[key] -= 1
            continue
        open_steps.append(step)
    return open_steps


def next_upgrade_step(state: "MatchState", exclude_role_id=None):
    """下一个该买券的升级步骤（按顺序，跳过已买未用和别人正在买的）。"""
    open_steps = _open_upgrade_steps(state, exclude_role_id)
    return open_steps[0] if open_steps else None


def upgrade_batch_size(state: "MatchState", name: str, exclude_role_id=None) -> int:
    """从下一个待买步骤起，连续需要同一种券的步骤数：按顺序一次买够这一段。"""
    count = 0
    for step in _open_upgrade_steps(state, exclude_role_id):
        if step["name"] != name:
            break
        count += 1
    return max(1, count)


def structure_priority_day(state: "MatchState") -> bool:
    """第三天起（day_index>=2）提高围墙和升基地优先级。"""
    return ((state.round_no or 0) // DAY_NIGHT_CYCLE) >= 2


def station_under_attack(state: "MatchState") -> bool:
    """机器人已贴到基地或进入近距，视为正在打基地。观测距离，不是官方射程表。"""
    station = own_station(state)
    if not station or station.health <= 0:
        return False
    from .tactics import threat_robots
    return any(chebyshev(station.pos, r.pos) <= 2 for r in threat_robots(state))


def station_voucher_use_now(state: "MatchState") -> bool:
    """基地券最好在挨打时用（升级回满血）。白天完好则先拿着；掉血、挨打或夜末再用。"""
    station = own_station(state)
    if not station or (station.level or 1) >= 2:
        return True
    if station.health < max_health(station):
        return True
    if station_under_attack(state):
        return True
    if is_day_round(state.round_no):
        return False
    cycle = (state.round_no or 0) % DAY_NIGHT_CYCLE
    return cycle >= DAY_NIGHT_CYCLE - 2


def all_weapons_level_at_least(state: "MatchState", level: int, need: int = 3) -> bool:
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    return len(weapons) >= need and all((w.level or 1) >= level for w in weapons)


def station_low_health_upgrade_pending(state: "MatchState") -> bool:
    """第二天起：两门武器已到2级且基地低于半血时，先升基地回血。"""
    station = own_station(state)
    if not station or (station.level or 1) >= 2:
        return False
    day = (state.round_no or 0) // DAY_NIGHT_CYCLE
    if day < 1:
        return False
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    if sum((w.level or 1) >= 2 for w in weapons) < 2:
        return False
    return station.health < max_health(station) * 0.5


def station_first_upgrade_pending(state: "MatchState") -> bool:
    """升级顺序走到基地 1→2：前面的步骤都已完成、已买券或有人在买，基地这一步还没买券。"""
    station = own_station(state)
    if not station or (station.level or 1) >= 2:
        return False
    steps, _surplus = upgrade_plan(state)
    claimed = _claimed_upgrade_steps(state)
    for step in steps:
        if step["kind"] == "station":
            return not step["covered"]
        key = _step_key(step)
        if step["covered"]:
            continue
        if claimed[key]:
            claimed[key] -= 1
            continue
        return False
    return False


def station_l2_upgrade_pending(state: "MatchState") -> bool:
    """基地 1→2：第二天低血抢救；第三天起优先；健康时仍要求三炮二级。"""
    station = own_station(state)
    if not station or (station.level or 1) >= 2:
        return False
    if station_low_health_upgrade_pending(state):
        return True
    if structure_priority_day(state):
        return True
    return station_first_upgrade_pending(state)


def self_evolution_work_open(state: "MatchState") -> bool:
    """开拓者有正在做或本回合能接的自进化任务时，不拿普通结构购物占用开拓者。
    任务点有任务但本回合接不了（回不了炮、夜里要守炮等）不算，否则开拓者会一直空转。"""
    if state.phase_task:
        return True
    if not state.team_our:
        return False
    from .pioneer_schedule import has_task_reservation
    if any(r.role_type == "pioneer" and r.health > 0 and has_task_reservation(state, r)
           for r in state.team_our.roles):
        return True
    ctx = getattr(state, "_pioneer_sched", None)
    if isinstance(ctx, dict) and "candidates" in ctx:
        return bool(ctx.get("selected"))
    return any(
        getattr(t, "is_valid", False)
        and getattr(t, "task_type", None) in ("自进化类1", "自进化类2")
        and (getattr(t, "cold_down_rounds", 0) or 0) == 0
        for t in (state.team_our.player_tasks or [])
    )


def station_first_buyer(state: "MatchState"):
    """三炮二级后的首次升基地：站在商店的工人优先，避免开拓者跑去抢单。"""
    if not state.team_our:
        return None
    shop = find_zone(state, "weaponShop")
    protect_pioneer = self_evolution_work_open(state)
    mobiles = [r for r in state.team_our.roles
               if r.role_type in ("worker", "pioneer") and r.health > 0
               and not (protect_pioneer and r.role_type == "pioneer")]
    if not mobiles and protect_pioneer:
        mobiles = [r for r in state.team_our.roles
                   if r.role_type in ("worker", "pioneer") and r.health > 0]
    if not mobiles:
        return None

    def key(role):
        if shop:
            dist = chebyshev(role.pos, shop.pos)
            on_shop = 0 if dist == 0 else 1
            at_shop = 0 if dist <= 1 else 1
        else:
            on_shop, at_shop, dist = 1, 1, 99
        kind = 0 if role.role_type == "worker" else 1
        return (on_shop, at_shop, kind, dist, role.id)

    return min(mobiles, key=key)


def weapon_upgrade_due(state: "MatchState") -> bool:
    """日程上是否还该升武器；不看当前是否已有人锁定买券任务。"""
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    if not weapons:
        return False
    from .opening import REQUIRED_OPENING_UPGRADES, critical_wall_missing
    day = (state.round_no or 0) // DAY_NIGHT_CYCLE
    l2 = sum((w.level or 1) >= 2 for w in weapons)
    if station_first_upgrade_pending(state):
        return False  # 升级顺序已走到基地这一步
    if any((w.level or 1) < 2 for w in weapons):
        if day <= 0:
            return l2 < REQUIRED_OPENING_UPGRADES
        if day == 1:
            if l2 < 2:
                return True
            return not station_l2_upgrade_pending(state)
        return True
    if station_first_upgrade_pending(state) or station_l2_upgrade_pending(state):
        return False
    if day <= 0:
        return False
    # 只有真正的关键缺口能压住武器升级；普通扩墙不设墙数门槛，否则升级会被无限推迟。
    if day == 1 and critical_wall_missing(state):
        return False
    return any((w.level or 1) < 3 for w in weapons)


def should_upgrade_weapon(state: "MatchState") -> bool:
    """按日程控制升级节奏。与 weapon_upgrade_due 共用日程；首日 WALL 阶段也允许筹资买券。"""
    from .opening import REQUIRED_OPENING_UPGRADES, live_l2_weapon_count
    from .opening_schedule import (
        STAGE_APPLY, STAGE_BUILD_WEAPONS, STAGE_FUND, STAGE_MUSTER, STAGE_WALL,
        current_opening_stage,
    )
    jobs = sum(1 for job in state.worker_item_jobs.values() if job.get("kind") == "weapon")
    day = (state.round_no or 0) // DAY_NIGHT_CYCLE
    if not weapon_upgrade_due(state):
        return False
    if day <= 0:
        stage = current_opening_stage(state)
        # 建炮中 / 回炮中不走全局卖矿催券；FUND/APPLY/WALL（首日墙先于升级）可买第一张。
        if stage in (STAGE_BUILD_WEAPONS, STAGE_MUSTER):
            return False
        if stage not in (STAGE_FUND, STAGE_APPLY, STAGE_WALL, None):
            return False
        if live_l2_weapon_count(state) >= REQUIRED_OPENING_UPGRADES:
            return False
        return jobs == 0
    return jobs == 0


def should_spend_surplus_on_upgrades(state: "MatchState") -> bool:
    """防线没有正面缺口时，别让金币躺着，尽快转成升级券。"""
    if not state.team_our or (state.team_our.gold_num or 0) < _WALL_VOUCHER_COST[1]:
        return False
    from .opening import critical_wall_missing
    return not critical_wall_missing(state)


def walls_still_to_build(state: "MatchState") -> bool:
    """还有新墙要建，且离入夜还早：此时修墙工的主线是建墙，不去升级。"""
    from .opening import critical_wall_missing, day_rounds_remaining, staged_walls_incomplete
    return bool((critical_wall_missing(state) or staged_walls_incomplete(state))
                and day_rounds_remaining(state.round_no) > WALL_UPGRADE_DUSK_WINDOW)


def wall_upgrade_jobs_allowed(state: "MatchState", role: Role) -> bool:
    """围墙升级/修复任务的统一闸门：先建新墙；第三天起经济工不接（涨价日集中升级阶段除外）。"""
    if walls_still_to_build(state):
        return False
    from .economy import spike_cashout_phase
    return (day3_worker_duty(state, role) != "economist"
            or spike_cashout_phase(state) == "upgrade")


def maybe_start_shop_item_job(role: Role, state: "MatchState", allow_weapon: bool = True,
                              allow_structure_upgrade: bool = True) -> None:
    """给空闲角色机会性分配一个"买道具->用道具"任务。"""
    if not state.team_our:
        return
    pending_targets = _pending_item_job_targets(state)
    trace(state, role.id, "shop_job_check", "检查维修与升级任务（预算为本回合尚未分配余额）",
          available_gold=state.team_our.gold_num, reserved_target_count=len(pending_targets))

    release_stale_repair_job(role, state)
    allow_wall_jobs = wall_upgrade_jobs_allowed(state, role)
    # 未购入任务按 升级武器 > 升墙/基地 > 紧急修墙 让位。已买到手的道具继续用完。
    old_job = state.worker_item_jobs.get(role.id)
    item_name = old_job.get('item', '') if old_job else ''
    if old_job and item_name not in role.backpack:
        plan_next = next_upgrade_step(state, exclude_role_id=role.id)
        weapon_due = plan_next["role"] if plan_next and plan_next["kind"] == "weapon" and plan_next["core"] else None
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
        elif (old_job.get('kind') == 'weapon' and plan_next is not None
              and (tuple(old_job['target']), item_name) != _step_key(plan_next)):
            trace(state, role.id, 'upgrade_job_preempted', '未购入的武器任务不是升级顺序上的下一步，重新按顺序领取',
                  old_item=item_name, next_item=plan_next["name"], next_target=plan_next["role"].id)
            del state.worker_item_jobs[role.id]
            pending_targets.discard(tuple(old_job['target']))
        elif (station_first_upgrade_pending(state) and old_job.get('kind') != 'station'):
            trace(state, role.id, 'upgrade_job_preempted',
                  '未购入的其它升级让位于三炮二级后的首次升基地',
                  old_kind=old_job.get('kind'))
            del state.worker_item_jobs[role.id]
            pending_targets.discard(tuple(old_job['target']))
    held = held_weapon_voucher_target(role, state, pending_targets)
    old_job = state.worker_item_jobs.get(role.id)
    if held and not (
            old_job and old_job.get('kind') == 'weapon' and old_job.get('item') in role.backpack):
        if old_job is None or old_job.get('item') not in role.backpack:
            name, weapon = held
            state.worker_item_jobs[role.id] = {"item": name, "target": (weapon.pos.x, weapon.pos.y), "kind": "weapon"}
            trace(state, role.id, 'held_weapon_voucher_job', '手里有武器券，立即用到同级武器上，不拿着不用',
                  item=name, weapon_id=weapon.id, weapon_level=weapon.level or 1)
            return
    if role.id in state.worker_item_jobs:
        return

    step = next_upgrade_step(state, exclude_role_id=role.id)
    if step and step["kind"] == "weapon" and step["core"] and allow_weapon:
        _assign_weapon_step(role, state, step)
        return

    station = own_station(state)
    if station_l2_upgrade_pending(state):
        if not station or (station.pos.x, station.pos.y) in pending_targets:
            return
        buyer = station_first_buyer(state)
        if buyer is not None and buyer.id != role.id:
            return
        if buyer is None and role.role_type not in ("worker", "pioneer"):
            return
        name, cost = voucher_for("station", station.level or 1)
        if name in role.backpack or state.team_our.gold_num >= item_cost(name, state):
            state.worker_item_jobs[role.id] = {
                "item": name, "target": (station.pos.x, station.pos.y), "kind": "station"}
            trace(state, role.id, 'station_after_weapons_l2',
                  '第三天优先升基地，或三门炮已到2级后先升一次基地', station_id=station.id)
            return
        trace(state, role.id, "station_upgrade_unaffordable", "应升基地，但余额不足",
              available_gold=state.team_our.gold_num, required_gold=cost)
        if not structure_priority_day(state):
            return

    wall = _pick_upgradeable(state, ("wall",), pending_targets,
                             below_health_ratio=WALL_UPGRADE_HEALTH_RATIO)
    if wall and allow_structure_upgrade and allow_wall_jobs:
        name, cost = voucher_for("wall", wall.level or 1)
        if name in role.backpack or state.team_our.gold_num >= item_cost(name, state):
            state.worker_item_jobs[role.id] = {"item": name, "target": (wall.pos.x, wall.pos.y), "kind": "wall"}
            return

    step = next_upgrade_step(state, exclude_role_id=role.id)
    if step and step["kind"] == "weapon" and allow_weapon:
        _assign_weapon_step(role, state, step)
        return

    if (allow_structure_upgrade and station and (station.level or 1) < 3
            and (station.pos.x, station.pos.y) not in pending_targets):
        name, cost = voucher_for("station", station.level or 1)
        from .treasure import shop_buy_allowed
        if name not in role.backpack and not shop_buy_allowed(name, state) and weapon_upgrade_due(state):
            trace(state, role.id, "early_buy_blocked", "武器升级仍有缺口，暂不买基地券", item=name)
        elif name in role.backpack or state.team_our.gold_num >= item_cost(name, state):
            state.worker_item_jobs[role.id] = {"item": name, "target": (station.pos.x, station.pos.y), "kind": "station"}
            return
        else:
            trace(state, role.id, "station_upgrade_unaffordable", "基地可升级，但余额不足", available_gold=state.team_our.gold_num, required_gold=cost)

    damaged_wall = _pick_damaged_wall(state, pending_targets)
    if damaged_wall and allow_wall_jobs and 'WallFixer' in role.backpack:
        state.worker_item_jobs[role.id] = {
            "item": "WallFixer", "target": (damaged_wall.pos.x, damaged_wall.pos.y), "kind": "wall",
        }



def held_weapon_voucher_target(role: Role, state: "MatchState", pending_targets: set):
    """手里的武器券立刻用在哪门武器上，返回 (券名, 武器) 或 None。
    券1只能升1级武器、券2只能升2级武器。按升级顺序（火箭A→火箭B→……）找第一门「现在正好是这个等级」的武器；
    顺序里没有（比如火箭A还是1级、手里却是券2）就给任意同级武器（火箭优先、近者优先）。
    只要有同级武器就一定有目标，从不"拿着等"。"""
    names = sorted({item for item in (role.backpack or [])
                    if isinstance(item, str) and item in ("WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2")})
    if not names:
        return None
    steps, _surplus = upgrade_plan(state)
    order = {}
    for i, step in enumerate(steps):
        if step["kind"] == "weapon":
            order.setdefault((step["role"].id, step["level"]), i)
    options = []
    for name in names:
        level = 1 if name.endswith("1") else 2
        for weapon in state.team_our.roles:
            if (weapon.role_type in WEAPON_TYPES and weapon.health > 0 and (weapon.level or 1) == level
                    and (weapon.pos.x, weapon.pos.y) not in pending_targets):
                options.append(((order.get((weapon.id, level), 999), _WEAPON_UPGRADE_ORDER.get(weapon.role_type, 99),
                                 chebyshev(role.pos, weapon.pos), weapon.id), name, weapon))
    if not options:
        if pending_targets:
            # 同级武器都被别人的任务锁着：锁不等于已经在用，照样给它，不能卡住
            return held_weapon_voucher_target(role, state, set())
        return None
    _key, name, weapon = min(options, key=lambda o: o[0])
    return name, weapon


def _assign_weapon_step(role: Role, state: "MatchState", step: dict) -> None:
    weapon, name = step["role"], step["name"]
    state.worker_item_jobs[role.id] = {"item": name, "target": (weapon.pos.x, weapon.pos.y), "kind": "weapon"}
    if name not in role.backpack and state.team_our.gold_num < item_cost(name, state):
        trace(state, role.id, 'weapon_upgrade_funding_gap', '已锁定升级顺序上的下一步，当前金币不足，禁止改做低优先级消费',
              weapon_id=weapon.id, current_level=weapon.level or 1, step_level=step["level"],
              available_gold=state.team_our.gold_num, required_gold=item_cost(name, state))


def _weapon_job_level_mismatch(state: "MatchState", job: dict) -> bool:
    """武器任务的券和目标武器等级对不上（被别人先升了），券用不出去。"""
    if job.get("kind") != "weapon":
        return False
    x, y = job["target"]
    weapon = next((r for r in state.team_our.roles
                   if r.role_type in WEAPON_TYPES and (r.pos.x, r.pos.y) == (x, y)), None)
    if weapon is None:
        return False
    item_level = 1 if job.get("item") == "WeaponUpgradeVoucher1" else 2
    return (weapon.level or 1) > item_level


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


def _team_item_count(state: "MatchState", item: str) -> int:
    return sum(
        (r.backpack or []).count(item)
        for r in (state.team_our.roles if state.team_our else [])
        if r.health > 0
    )


def _batch_buy_quantity(role: Role, state: "MatchState", job: dict) -> int:
    item = job.get("item")
    cost = item_cost(item, state)
    if not item or cost <= 0:
        return 1
    cap = role.back_pack_capability or 0
    free = max(0, cap - len(role.backpack or [])) if cap else 1
    affordable = max(0, (state.team_our.gold_num or 0) // cost)
    if free <= 0 or affordable <= 0:
        return 0
    pending = _pending_item_job_targets(state)
    held = _team_item_count(state, item)
    desired = 1
    if item in ("WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2"):
        # 升级计划已经扣掉全队已买未用的券，这里不再减。
        return max(1, min(upgrade_batch_size(state, item, exclude_role_id=role.id), affordable, free))
    elif item == "StationUpgradeVoucher1":
        station = own_station(state)
        desired = 1 if station and (station.level or 1) <= 1 else 0
    elif item == "StationUpgradeVoucher2":
        station = own_station(state)
        desired = 1 if station and (station.level or 1) == 2 else 0
    elif item in ("WallUpgradeVoucher1", "WallUpgradeVoucher2"):
        # 只按“本任务这面 + 其它未被认领、低于半血的同级墙”买，不给健康墙囤券。
        level = 1 if item == "WallUpgradeVoucher1" else 2
        others = pending - {tuple(job.get("target") or ())}
        desired = sum(
            1 for w in state.team_our.roles
            if w.role_type == "wall" and w.health > 0 and (w.level or 1) == level
            and (w.pos.x, w.pos.y) not in others
            and w.health < max_health(w) * WALL_UPGRADE_HEALTH_RATIO
        )
    desired = max(1, desired - held)
    return max(1, min(desired, affordable, free))


def decide_shop_item_job(role: Role, state: "MatchState", blocked: set, reserved: set):
    """推进一个已分配的两段式任务：没道具先去商店买，有道具就走到目标建筑一格内使用。"""
    job = state.worker_item_jobs.get(role.id)
    if not job:
        return None
    if not _job_target_still_exists(state, job):
        trace(state, role.id, "job_target_missing", "道具任务目标建筑已不存在，释放任务")
        del state.worker_item_jobs[role.id]
        return None
    if job.get("item") in role.backpack and _weapon_job_level_mismatch(state, job):
        trace(state, role.id, "weapon_job_level_mismatch", "目标武器等级已变，券对不上，重新挑同级武器")
        del state.worker_item_jobs[role.id]
        maybe_start_shop_item_job(role, state)
        job = state.worker_item_jobs.get(role.id)
        if not job:
            return None

    # 旧存档中尚未购买的基地升级任务，让位于未完成的二级武器/城墙；三炮二级后或第三天的升基地不让位。
    if (job.get("kind") == "station" and job["item"] not in role.backpack
            and not station_l2_upgrade_pending(state)):
        available = _pending_item_job_targets(state) - {tuple(job["target"])}
        defense = _pick_upgradeable(state, WEAPON_TYPES + ("wall",), available, max_current_level=1)
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

    def route(goal):
        if is_day_round(state.round_no):
            return adjacent_path(role, goal, walkable, state)
        from .opening import courtyard_path, night_safe_path
        if goal == target and job.get("kind") == "wall":
            inside = courtyard_path(role, goal, walkable, state)
            if inside is not None:
                return inside  # 夜里修墙尽量不出院子
        return night_safe_path(role, goal, walkable, state)

    if item in role.backpack:
        if job.get("kind") == "station" and item == "StationUpgradeVoucher1" and not station_voucher_use_now(state):
            trace(state, role.id, "station_voucher_hold_for_attack",
                  "基地券留到挨打或火箭冷却时再用，升级回满血收益更高")
            return None
        if chebyshev(role.pos, target) <= 1:
            job["awaiting_use"] = True
            return selected(state, role.id, {"action": "use", "name": item, "targetPos": [{"x": x, "y": y}]}, '执行维修/升级道具任务')
        if job.get("kind") == "weapon" and worker_defers_voucher_use(role, state, blocked):
            trace(state, role.id, "weapon_voucher_deferred", "券先拿着，背包有空继续干活，回防时顺路用掉", item=item)
            return None
        path = route(target)
        if item != "WallFixer" and path is not None:
            from .economy import en_route_collect
            detour = en_route_collect(role, state, len(path), '带着升级券回家，顺路采矿；入夜前仍来得及回去使用')
            if detour:
                return detour
        return move_on_path(state, role, path, reserved, '执行维修/升级道具任务')

    if state.team_our.gold_num < item_cost(item, state):
        from .opening import survival_walls_locked
        day0 = (state.round_no or 0) // DAY_NIGHT_CYCLE == 0
        trace(state, role.id, "insufficient_gold", "道具任务购买资金不足，释放任务", available_gold=state.team_our.gold_num, required_gold=item_cost(item, state), item=item)
        if job.get('kind') == 'weapon' and not day0 and not survival_walls_locked(state):
            trace(state, role.id, 'weapon_upgrade_job_waiting_funds', '保留武器升级目标并继续筹资，不改做城墙/基地升级')
            return None
        del state.worker_item_jobs[role.id]
        return None
    from .treasure import shop_buy_allowed
    if not shop_buy_allowed(item, state):
        trace(state, role.id, "early_buy_blocked", "第四天前不买该商店道具，金币留给武器升级", item=item)
        if job.get("kind") != "weapon":
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
        num = _batch_buy_quantity(role, state, job)
        if num <= 0:
            return None
        return selected(state, role.id, {"action": "buy", "name": item, "num": num}, '批量购买升级券并按队列逐个使用')
    return move_on_path(state, role, route(shop.pos), reserved, '执行维修/升级道具任务')



def day3_worker_duty(state: "MatchState", worker: Role) -> Optional[str]:
    """第三天起沿用首日分工：施工工专职修墙/升墙（keeper），经济工专职采卖矿和武器券（economist）。
    只剩一名工人时由他兼任 keeper。"""
    if worker.role_type != "worker" or not structure_priority_day(state):
        return None
    from .opening_schedule import opening_worker_mode
    return "keeper" if opening_worker_mode(state, worker) == "builder" else "economist"


def _night_repair_reachable(role: Role, state: "MatchState", blocked: set, reserved: set, wall: Role) -> bool:
    """夜里修墙“都在家”：只修从院子里够得着的墙。手里没有道具时，
    有防守压力就不出门买；没有压力才允许走安全路线去商店。"""
    from .opening import courtyard_path, mobile_walkable, night_strict_path
    from .tactics import front_breached, pressure
    walkable = mobile_walkable(state, blocked, reserved)
    if courtyard_path(role, wall.pos, walkable, state) is None:
        return False
    level = wall.level or 1
    item = voucher_for("wall", level)[0] if level < 3 else "WallFixer"
    if item in role.backpack:
        return True
    if pressure(state) or front_breached(state):
        return False
    shop = find_zone(state, "weaponShop")
    return bool(shop and night_strict_path(role, shop.pos, walkable, state) is not None)


def maintain_front_wall_health(role: Role, state: "MatchState", blocked: set, reserved: set):
    """第三天起由专职修墙工保持前排墙半血以上；低于半血优先升级回血。"""
    if role.role_type != "worker" or not state.team_our:
        return None
    if day3_worker_duty(state, role) != "keeper":
        return None
    night = not is_day_round(state.round_no)
    job = state.worker_item_jobs.get(role.id)
    if job and job.get("kind") == "wall":
        # 已有修墙任务就接着做；夜里那面墙从院子里够不着了就放弃。
        wall = next((r for r in state.team_our.roles
                     if r.role_type == "wall" and r.health > 0
                     and (r.pos.x, r.pos.y) == tuple(job["target"])), None)
        if wall is not None and (not night or _night_repair_reachable(role, state, blocked, reserved, wall)):
            return decide_shop_item_job(role, state, blocked, reserved)
        if job.get("item") not in role.backpack:
            del state.worker_item_jobs[role.id]
        return None
    if job and (job.get("kind") == "weapon" or job.get("item") in role.backpack):
        # 手里已有道具或正在升武器：不顶掉，先把当前任务做完。
        return None
    if any(isinstance(item, str) and item.startswith("WeaponUpgradeVoucher") for item in (role.backpack or [])):
        return None
    pending = _pending_item_job_targets(state)
    from .opening import staged_walls_incomplete, critical_wall_missing, day_rounds_remaining
    new_walls_due = staged_walls_incomplete(state) or critical_wall_missing(state)
    early_day = is_day_round(state.round_no) and day_rounds_remaining(state.round_no) > WALL_UPGRADE_DUSK_WINDOW
    if new_walls_due and early_day:
        # 白天还早且新墙没齐：不跑商店升半血墙，只修即将倒塌的。
        wall = _pick_damaged_wall(state, pending)
        if wall is None:
            return None
        if night and not _night_repair_reachable(role, state, blocked, reserved, wall):
            return None
    else:
        while True:
            wall = _pick_front_wall_below_floor(state, pending, floor=WALL_UPGRADE_HEALTH_RATIO)
            if wall is None or not night or _night_repair_reachable(role, state, blocked, reserved, wall):
                break
            pending = pending | {(wall.pos.x, wall.pos.y)}
        if wall is None:
            return None
    level = wall.level or 1
    item = voucher_for("wall", level)[0] if level < 3 else "WallFixer"
    if item not in role.backpack and state.team_our.gold_num < item_cost(item, state):
        trace(state, role.id, "front_wall_health_unfunded",
              "前排墙低于半血但金币不足，无法立刻购买升级/修复道具",
              wall_id=wall.id, wall_health=wall.health, wall_level=level,
              item=item, gold=state.team_our.gold_num)
        return None
    state.worker_item_jobs[role.id] = {
        "item": item, "target": (wall.pos.x, wall.pos.y), "kind": "wall",
    }
    cmd = decide_shop_item_job(role, state, blocked, reserved)
    if cmd:
        trace(state, role.id, "front_wall_health_maintenance",
              "前排墙低于半血，先升级/修复回血，再继续补新墙",
              wall_id=wall.id, wall_health=wall.health, wall_level=level, item=item)
    return cmd


def spike_day_cashout(worker: Role, state: "MatchState", blocked: set, reserved: set):
    """矿价上涨日：经济工白天先清空背包，再买券回家升级武器/基地/围墙，做完再去采矿。"""
    from .economy import (
        liquidate, muster_for_night, sellable_ores, set_spike_cashout_phase, spike_cashout_phase,
    )
    from .opening_schedule import opening_worker_mode
    phase = spike_cashout_phase(state)
    if phase in (None, "done") or worker.role_type != "worker":
        return None
    if opening_worker_mode(state, worker) != "economist":
        return None
    if phase == "sell":
        if sellable_ores(worker, state, dump_extra_stone=True, ignore_stockpile=True):
            handled, cmd = liquidate(worker, state, blocked, reserved,
                                     force_reason="矿价上涨日，经济工先清空背包")
            if cmd:
                trace(state, worker.id, "spike_day_cashout", "矿价上涨日清包变现", phase="sell")
                return cmd
            if handled:
                return None  # 小贩暂时不可达：保持清包阶段，交给常规流程。
        set_spike_cashout_phase(state, "upgrade")
        trace(state, worker.id, "spike_day_phase", "清包完成或来不及卖，转入买券升级", phase="upgrade")
    handled, cmd = muster_for_night(worker, state, blocked, reserved)
    if handled:
        return cmd
    for _ in range(3):
        maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=True)
        if worker.id not in state.worker_item_jobs:
            break
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            trace(state, worker.id, "spike_day_cashout", "矿价上涨日买券并升级武器/基地/围墙",
                  phase="upgrade", item=state.worker_item_jobs.get(worker.id, {}).get("item"))
            return cmd
    job = state.worker_item_jobs.get(worker.id)
    if job and (job.get("item") in worker.backpack
                or state.team_our.gold_num >= item_cost(job.get("item"), state)):
        return None  # 券在手或买得起但本回合走不了，保持升级阶段，由常规流程继续推进。
    set_spike_cashout_phase(state, "done")
    trace(state, worker.id, "spike_day_phase", "金币已用完或没有可升级目标，回去采矿", phase="done",
          gold=state.team_our.gold_num)
    return None


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
    last_failed = (state.last_round_role_action_results or {}).get(worker.id) is False
    if pending and pending[2] == "wall":
        from .opening import due_wall_gaps, failed_move_cells
        if "stone" not in (worker.backpack or []):
            del state.worker_build_targets[worker.id]
            pending = None
        elif last_failed or pending[:2] not in due_wall_gaps(state, worker):
            del state.worker_build_targets[worker.id]
            pending = None
        elif (pending[0], pending[1], "wall") in state.failed_build_spots:
            del state.worker_build_targets[worker.id]
            pending = None
        elif worker is not None:
            better = pick_build_target(state, base.pos, (blocked | reserved) - {(worker.pos.x, worker.pos.y)},
                                       "wall", worker=worker)
            stuck = (pending[0], pending[1]) in failed_move_cells(state, worker)
            if better is not None and (
                    stuck
                    or chebyshev(worker.pos, Pos(*pending[:2])) > chebyshev(worker.pos, better) + 1):
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
            pending = None
        else:
            target = Pos(x, y)
            dist = chebyshev(worker.pos, target)
            if dist == 0:
                from .opening import step_off_construction
                cmd = step_off_construction(worker, state, blocked, reserved)
                if cmd:
                    return cmd
                return None
            if dist == 1:
                if kind == "wall":
                    from .opening import worker_should_build_walls
                    if not worker_should_build_walls(state, worker):
                        del state.worker_build_targets[worker.id]
                        pending = None
                    else:
                        del state.worker_build_targets[worker.id]
                        if "stone" not in worker.backpack:
                            return None
                        from .opening import safe_wall, assign_weapons
                        if not safe_wall(state, (x, y), blocked | reserved, assign_weapons(state)):
                            other = pick_build_target(
                                state, base.pos, (blocked | reserved | {(x, y)}) - {(worker.pos.x, worker.pos.y)},
                                "wall", worker=worker)
                            if other is not None:
                                trace(state, worker.id, 'wall_route_blocked', '施工会截断通路，改选其它缺口')
                                pending = None
                            else:
                                reserved.add((x, y))
                                return selected(
                                    state, worker.id,
                                    {"action": "build", "name": "wall", "targetPos": [{"x": x, "y": y}]},
                                    '贴着缺口且没有别的可砌格，先封上避免空转')
                        else:
                            name = "wall"
                            reserved.add((x, y))
                            return selected(state, worker.id, {"action": "build", "name": name, "targetPos": [{"x": x, "y": y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
                else:
                    del state.worker_build_targets[worker.id]
                    if state.team_our.gold_num < WEAPON_GOLD_COST:
                        return None
                    name = pick_weapon_name(state)
                    reserved.add((x, y))
                    return selected(state, worker.id, {"action": "build", "name": name, "targetPos": [{"x": x, "y": y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
            elif kind == "wall":
                from .opening import wall_approach_path, move_on_path, step_toward_wall_gap
                path = wall_approach_path(worker, target, blocked | reserved, state)
                if path is None:
                    greedy = step_toward_wall_gap(worker, (x, y), blocked, reserved, state)
                    if greedy:
                        cmd = move_on_path(state, worker, greedy, reserved, 'BFS接近失败，朝缺口迈一步避免空转')
                        if cmd:
                            return cmd
                    del state.worker_build_targets[worker.id]
                    pending = None
                else:
                    cmd = move_on_path(state, worker, path, reserved, '从院内接近城墙缺口')
                    if cmd:
                        return cmd
                    del state.worker_build_targets[worker.id]
                    pending = None
            else:
                step = traced_move(state, worker.id, worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
                if step:
                    reserved.add((step.x, step.y))
                    return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
                return None

    # pending 仍在说明这一回合已经处理过建造目标（走到半路），不要另开新目标。
    if pending:
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
    target = pick_build_target(state, base.pos, (blocked | reserved | pending_spots) - own, kind,
                               worker=worker if kind == "wall" else None)
    if target is None:
        trace(state, worker.id, "no_build_candidate", "搜索范围内无可用建造候选格（占用、越界或失败冷却）", kind=kind)
        return None
    state.worker_build_targets[worker.id] = (target.x, target.y, kind)
    reserved.add((target.x, target.y))
    if kind == "wall":
        from .opening import wall_approach_path, move_on_path, step_off_construction
        dist = chebyshev(worker.pos, target)
        if dist == 0:
            cmd = step_off_construction(worker, state, blocked, reserved)
            if cmd:
                return cmd
            del state.worker_build_targets[worker.id]
            return None
        if dist == 1:
            del state.worker_build_targets[worker.id]
            if "stone" not in worker.backpack:
                return None
            reserved.add((target.x, target.y))
            return selected(state, worker.id, {"action": "build", "name": "wall", "targetPos": [{"x": target.x, "y": target.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
        path = wall_approach_path(worker, target, blocked | reserved, state)
        cmd = move_on_path(state, worker, path, reserved, '从院内接近城墙缺口')
        if cmd:
            return cmd
        from .opening import step_toward_wall_gap
        greedy = step_toward_wall_gap(worker, (target.x, target.y), blocked, reserved, state)
        if greedy:
            cmd = move_on_path(state, worker, greedy, reserved, 'BFS接近失败，朝缺口迈一步避免空转')
            if cmd:
                return cmd
        del state.worker_build_targets[worker.id]
        return None
    step = traced_move(state, worker.id, worker.pos, target, blocked | reserved, state.map_info.width, state.map_info.height)
    if step:
        reserved.add((step.x, step.y))
        return selected(state, worker.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, '执行建造计划：补足武器优先，其次用石头建墙')
    return None



def decide_pioneer_voucher(pioneer: Role, state: "MatchState", blocked: set, reserved: set,
                           allow_interrupt=False, interrupt_reason=None):
    """开拓者买/用武器升级券。进行中的任务和普通任务预约默认不中断。"""
    if pioneer.role_type != "pioneer" or pioneer.health <= 0:
        return None
    if state.phase_task:
        add_branch(state, 'voucher_blocked_by_phase_task')
        return None
    if has_task_reservation(state, pioneer) and not allow_interrupt:
        add_branch(state, 'voucher_blocked_by_reservation')
        trace(state, pioneer.id, 'voucher_blocked_by_reservation',
              '进行中的任务预约占用开拓者，不新开普通买券',
              reservation=reservation_of(state))
        return None
    reservation = reservation_of(state)
    job = state.worker_item_jobs.get(pioneer.id)
    has_voucher = any(isinstance(item, str) and "WeaponUpgradeVoucher" in item for item in pioneer.backpack)
    other_weapon_job = next(
        (rid for rid, item in state.worker_item_jobs.items()
         if item.get('kind') == 'weapon' and rid != pioneer.id),
        None,
    )
    if job and job.get("kind") != "weapon" and not has_voucher:
        trace(state, pioneer.id, 'voucher_skip_other_job',
              '开拓者已有非武器采购任务，不改去买券', jobKind=job.get('kind'), item=job.get('item'))
        return None
    if has_voucher or (job and job.get("kind") == "weapon"):
        if has_voucher and (not job or job.get("kind") != "weapon"):
            maybe_start_shop_item_job(pioneer, state, allow_weapon=True)
        cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
        if cmd:
            if has_task_reservation(state, pioneer) and allow_interrupt:
                interrupt_reservation(state, interrupt_reason or 'defense_critical_voucher_job',
                                      extra={'action': cmd.get('action')})
            trace(state, pioneer.id, "pioneer_voucher_job",
                  "开拓者执行武器升级券任务（购买或使用）", action=cmd.get("action"),
                  interrupt=bool(has_task_reservation(state, pioneer) and allow_interrupt),
                  interruptReason=interrupt_reason)
        return cmd
    from .economy import pick_weapon_voucher_buyer
    extra = bool(other_weapon_job) and should_upgrade_weapon(state)
    buyer = pick_weapon_voucher_buyer(state, blocked, extra=extra)
    if buyer is None or buyer.id != pioneer.id:
        reason = 'no_buyer' if buyer is None else 'other_buyer'
        trace(state, pioneer.id, 'voucher_skip_not_selected',
              '未选中开拓者买券：已有其他人负责或当前无人能按时完成',
              skipReason=reason, buyer_id=None if buyer is None else buyer.id,
              other_weapon_job=other_weapon_job, available_gold=state.team_our.gold_num,
              upgradeDue=should_upgrade_weapon(state) or bool(other_weapon_job))
        return None
    if not should_upgrade_weapon(state):
        trace(state, pioneer.id, 'voucher_skip_not_due',
              '当前没有合法武器升级目标或日程未到', available_gold=state.team_our.gold_num)
        return None
    weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=2)
    if weapon is None:
        trace(state, pioneer.id, 'voucher_skip_no_target',
              '没有可升级的存活武器', available_gold=state.team_our.gold_num)
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
        if has_task_reservation(state, pioneer) and allow_interrupt:
            interrupt_reservation(state, interrupt_reason or 'defense_critical_voucher',
                                  cost=cost, extra={'item': name})
        trace(state, pioneer.id, "pioneer_buys_voucher", "任务金币已够，开拓者去买武器升级券",
              item=name, available_gold=state.team_our.gold_num, required_gold=cost,
              interrupt=bool(has_task_reservation(state, pioneer) and allow_interrupt),
              interruptReason=interrupt_reason, defenseCritical=bool(allow_interrupt))
    return cmd


def builder_on_walls(state: "MatchState", worker: Role) -> bool:
    """第一晚之后，开局定的施工工在生存墙没齐、或夜前还能补一段侧翼时专心修墙。只剩一名工人时不锁定。"""
    if worker.role_type != "worker" or not is_day_round(state.round_no):
        return False
    if sum(1 for r in state.team_our.roles if r.role_type == "worker" and r.health > 0) < 2:
        return False
    if sum(1 for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0) < MAX_WEAPONS:
        return False
    from .opening_schedule import opening_worker_mode
    if opening_worker_mode(state, worker) != "builder":
        return False
    base = own_station(state)
    if base is None:
        return False
    from .opening import due_wall_gaps
    return bool(due_wall_gaps(state, worker))


def decide_worker_day(worker: Role, state: "MatchState", blocked: set, reserved: set):
    from .economy import (
        in_pre_night_cashout_window, liquidate, profitable_mine, muster_for_night,
        worker_should_shop_weapon_voucher,
    )
    from .tactics import tactical_action
    from .opening import (
        critical_wall_missing, replenish_walls, staged_walls_incomplete,
        worker_should_build_walls, emergency_front_seal,
    )
    heal = decide_emergency_heal(worker, state)
    if heal:
        return selected(state, worker.id, heal, '低血紧急治疗')
    seal = emergency_front_seal(worker, state, blocked, reserved)
    if seal:
        return seal
    release_stale_repair_job(worker, state)
    spike = spike_day_cashout(worker, state, blocked, reserved)
    if spike:
        return spike
    front_wall = maintain_front_wall_health(worker, state, blocked, reserved)
    if front_wall:
        return front_wall
    allow_build = worker_should_build_walls(state, worker)
    allow_weapon = worker_should_shop_weapon_voucher(worker, state, blocked)
    builder_focus = builder_on_walls(state, worker)
    if builder_focus:
        shop = find_zone(state, "weaponShop")
        at_shop = bool(shop and chebyshev(worker.pos, shop.pos) <= 1)
        holds_voucher = any(isinstance(i, str) and i.startswith('WeaponUpgradeVoucher')
                            for i in (worker.backpack or []))
        if not (at_shop or holds_voucher):
            allow_weapon = False  # 施工工防线没修完不专程去买券；人在商店边顺手买、手里有券照常用
    from .opening_schedule import opening_worker_mode
    eco_mode = opening_worker_mode(state, worker) == 'economist'
    # 第三天起经济工清包买券；第一晚后经济工就不接普通补墙，只在正面关键缺口时帮一把。
    economist = eco_mode and structure_priority_day(state)
    guns_ready = sum(1 for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0) >= MAX_WEAPONS
    # 三炮未齐时先补武器；施工工补墙，经济工只在正面缺口帮忙。
    wall_help = guns_ready and (not eco_mode or critical_wall_missing(state))
    cashout = False if builder_focus else in_pre_night_cashout_window(worker, state, blocked, reserved)
    job = state.worker_item_jobs.get(worker.id)
    held_item = bool(job and job.get('item') in worker.backpack)
    held_weapon_voucher = any(
        isinstance(item, str) and item.startswith('WeaponUpgradeVoucher')
        for item in (worker.backpack or [])
    )
    if cashout:
        handled, cmd = liquidate(worker, state, blocked, reserved)
        if cmd:
            return cmd
        if allow_weapon or held_item:
            if allow_weapon:
                maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=False)
            cmd = decide_shop_item_job(worker, state, blocked, reserved)
            if cmd:
                return cmd
    if allow_weapon and (held_weapon_voucher or (job and job.get('kind') == 'weapon')):
        maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=False)
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            trace(state, worker.id, 'pre_muster_weapon_voucher',
                  '白天回防前优先推进武器升级券，避免券带进夜里不用',
                  heldVoucher=held_weapon_voucher, jobKind=(job or {}).get('kind'))
            return cmd
    # 施工工墙没砌完且还来得及建：白天不提前回炮，只在入夜窗口才去双火箭。
    if not (builder_focus and allow_build):
        handled, cmd = muster_for_night(worker, state, blocked, reserved)
        if handled:
            return cmd
    if builder_focus:
        # 施工工主线：采石 → 攒够回家 → 建墙，不被卖矿/买券/经济任务来回拉走。
        if allow_weapon:  # 只剩“人在商店边顺手买”或“手里有券”两种情况
            maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=False)
            cmd = decide_shop_item_job(worker, state, blocked, reserved)
            if cmd:
                return cmd
        handled, cmd = replenish_walls(worker, state, blocked, reserved, primary_only=False, allow_build=allow_build)
        if cmd:
            return cmd
        if handled:
            from .opening import builder_move_to_dual_rockets, builder_unjam_walls
            from .economy import go_mine
            cmd = builder_unjam_walls(worker, state, blocked, reserved, allow_mine=True)
            if cmd:
                return cmd
            if not allow_build:
                cmd = builder_move_to_dual_rockets(
                    worker, state, blocked, reserved, '入夜前不够再建一段，施工工去双火箭位待命')
                if cmd:
                    return cmd
            if 'stone' not in (worker.backpack or []):
                cmd = go_mine(
                    worker, state, blocked, reserved, want_ores=('stone',), purpose='stone',
                    travel_reason='防线尚未完成，专程采石',
                    collect_reason='采集下一段城墙所需石料',
                )
                if cmd:
                    return cmd
                cmd = profitable_mine(worker, state, blocked, reserved)
                if cmd:
                    return cmd
            heal = decide_self_heal(worker) or decide_buy_medicine(worker, state)
            if heal:
                return heal
            trace(state, worker.id, 'builder_waiting_on_walls',
                  '施工工本回合墙动作发不出，改走后续采矿/经济，避免原地空转',
                  allow_build=allow_build, stones=worker.backpack.count('stone'))
            # 不要 return None：后面 profitable_mine 还能干活。
    # 经济工墙缺口：先清包（策略 4.3），再继续道具任务 / 买券。
    if economist and (staged_walls_incomplete(state) or critical_wall_missing(state)):
        from .economy import sellable_ores
        if sellable_ores(worker, state, ignore_stockpile=True):
            handled, cmd = liquidate(worker, state, blocked, reserved,
                                     force_reason='第三天经济工先清空背包，再判断金币够不够升级',
                                     keep_wall_stone=True)
            if cmd:
                return cmd
    continue_job = False
    if job:
        if job.get('kind') == 'station' and job.get('item') in worker.backpack:
            continue_job = station_voucher_use_now(state)
        elif held_item:
            continue_job = True
        elif job.get('kind') == 'weapon':
            continue_job = True
        elif staged_walls_incomplete(state) and allow_build:
            continue_job = False
        else:
            continue_job = True
    if continue_job:
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            return cmd
    if economist and (staged_walls_incomplete(state) or critical_wall_missing(state)):
        # 墙有缺口时由施工工补墙；经济工按到手金币决定买券升级还是采矿。
        maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=True)
        eco_job = state.worker_item_jobs.get(worker.id)
        if eco_job and (eco_job.get('item') in worker.backpack
                        or state.team_our.gold_num >= item_cost(eco_job.get('item'), state)):
            cmd = decide_shop_item_job(worker, state, blocked, reserved)
            if cmd:
                trace(state, worker.id, 'economist_upgrade_during_wall_gap',
                      '墙有缺口由施工工补，经济工先用现有金币买券升级',
                      item=eco_job.get('item'), gold=state.team_our.gold_num)
                return cmd
        mine = profitable_mine(worker, state, blocked, reserved)
        if mine:
            trace(state, worker.id, 'economist_mines_during_wall_gap',
                  '墙缺口由施工工处理，经济工没有可执行升级动作，继续采矿避免白天空转',
                  gold=state.team_our.gold_num, job_kind=(eco_job or {}).get('kind'))
            return mine
    if structure_priority_day(state) and wall_help and (not cashout or critical_wall_missing(state)):
        handled, cmd = replenish_walls(worker, state, blocked, reserved, primary_only=False, allow_build=allow_build)
        if cmd:
            return cmd
        maybe_start_shop_item_job(worker, state, allow_weapon=False, allow_structure_upgrade=True)
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            return cmd
    if allow_weapon:
        maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=False)
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            return cmd
        handled, cmd = liquidate(worker, state, blocked, reserved)
        if cmd:
            return cmd
    if should_spend_surplus_on_upgrades(state):
        maybe_start_shop_item_job(worker, state, allow_weapon=True, allow_structure_upgrade=True)
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            trace(state, worker.id, 'surplus_upgrade_spend',
                  '正面无紧急缺口，优先把闲置金币转成武器/基地/围墙升级',
                  gold=state.team_our.gold_num)
            return cmd
    if station_first_upgrade_pending(state):
        maybe_start_shop_item_job(worker, state, allow_weapon=allow_weapon)
        cmd = decide_shop_item_job(worker, state, blocked, reserved)
        if cmd:
            return cmd
    if wall_help and (not cashout or critical_wall_missing(state)):
        handled, cmd = replenish_walls(worker, state, blocked, reserved, primary_only=True, allow_build=allow_build)
        if cmd:
            return cmd
    handled, cmd = liquidate(worker, state, blocked, reserved)
    if cmd:
        return cmd
    defer_upgrades = (weapon_upgrade_due(state)
                      or ((state.round_no or 0) >= 70 and staged_walls_incomplete(state)))
    if structure_priority_day(state):
        defer_upgrades = False
    maybe_start_shop_item_job(
        worker, state, allow_weapon=allow_weapon,
        allow_structure_upgrade=not defer_upgrades,
    )
    cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if cmd:
        return cmd
    if not cashout and not eco_mode and sum(r.role_type in WEAPON_TYPES for r in state.team_our.roles) >= 3:
        handled, cmd = replenish_walls(worker, state, blocked, reserved, allow_build=allow_build)
        if cmd:
            return cmd
    cmd = tactical_action(worker, state, blocked, reserved)
    if cmd:
        return cmd

    item_job_cmd = decide_shop_item_job(worker, state, blocked, reserved)
    if item_job_cmd:
        return item_job_cmd

    if not cashout:
        from .opening import should_gather_wall_stone
        if not should_gather_wall_stone(worker, state):
            build_cmd = try_build(worker, state, blocked, reserved)
            if build_cmd:
                return build_cmd

    final_cmd = profitable_mine(worker, state, blocked, reserved) or decide_self_heal(worker) or decide_buy_medicine(worker, state)
    if not final_cmd:
        trace(state, worker.id, 'worker_day_no_command', '第二天及以后白天流程走完仍无命令',
              allow_build=allow_build, allow_weapon=allow_weapon, cashout=cashout,
              held_item=held_item, job_kind=(job or {}).get('kind'),
              structure_priority_day=structure_priority_day(state),
              gold=state.team_our.gold_num if state.team_our else None,
              backpack=list(worker.backpack or []), position={'x': worker.pos.x, 'y': worker.pos.y})
    return final_cmd



def decide_pioneer_task(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """进行中的任务一直待到做完（离开任务点即失败）。
    未开始的任务受 defense_due 约束；普通买券不抢占可行任务。"""
    from .economy import defense_due
    add_branch(state, 'decide_pioneer_task')
    if pioneer.health <= 0:
        mark_outcome(state, 'dead', None, 'dead')
        return True, None
    ctx = getattr(state, '_pioneer_sched', None)
    if not isinstance(ctx, dict) or 'candidates' not in ctx:
        ctx = begin_schedule(state, pioneer, blocked, reserved)
    if state.phase_task:
        add_branch(state, 'active_phase_task')
        # 规则：任务开始后离开任务点一格外就直接失败，所以一旦开始就待到做完或超时。
        heal = decide_emergency_heal(pioneer, state) or decide_self_heal(pioneer)
        mark_outcome(state, 'hold_active_task', heal, 'hold_active_task')
        bump_streak(state, 'task')
        return True, heal
    reservation = reservation_of(state)
    if reservation and reservation.get('stage') == 'accept_pending':
        add_branch(state, 'accept_pending')
        if defense_due(pioneer, state, blocked):
            add_branch(state, 'accept_pending_yield_defense')
            mark_outcome(state, 'task_yields_to_defense', None, 'yield_pending_to_defense')
            trace(state, pioneer.id, 'task_yields_to_defense',
                  '领取待确认时回防已到期，先回炮，不把待确认当成已占领')
            return False, None
        prev = (state.last_sent_command or {}).get(pioneer.id) or {}
        result = (state.last_round_role_action_results or {}).get(pioneer.id)
        if prev.get('action') == 'acceptTask' and result is not False:
            mark_outcome(state, 'accept_pending', None, 'accept_pending')
            bump_streak(state, 'task')
            return True, None
    row = ctx.get('selected')
    if row and (not reservation or reservation.get('stage') == 'approaching'):
        from .pioneer_schedule import save_reservation
        save_reservation(state, pioneer, row, stage='approaching')
    if defense_due(pioneer, state, blocked):
        add_branch(state, 'new_task_defense_gate')
        mark_outcome(state, 'task_yields_to_defense', None, 'defense_blocks_new_task')
        trace(state, pioneer.id, 'task_yields_to_defense', '回防时间已到或家中告急，不再新接任务',
              schedulerVersion=SCHEDULER_VERSION,
              defense=ctx.get('defense'))
        return False, None
    if not row:
        add_branch(state, 'no_feasible_task')
        for candidate in ctx.get('candidates') or []:
            reason = candidate.get('rejected')
            if reason == 'defense_time':
                trace(state, pioneer.id, 'task_not_enough_time',
                      '预计解题耗时加回防余量不足，不接这单',
                      task_type=candidate.get('taskType'), needed=candidate.get('needed'),
                      threat_eta=candidate.get('available'), solve_estimate=candidate.get('solveEstimate'),
                      solve_source=candidate.get('solveSource'), timeout_rounds=candidate.get('timeoutRounds'))
            elif reason == 'platform_timeout_too_short':
                trace(state, pioneer.id, 'task_timeout_too_short',
                      '平台给出的时限不够完成探查、修复与提交，不接这单',
                      task_type=candidate.get('taskType'), timeout_rounds=candidate.get('timeoutRounds'))
            elif reason == 'solve_exceeds_platform_timeout':
                trace(state, pioneer.id, 'task_timeout_too_short',
                      '预计解题耗时超过平台时限，不接这单',
                      task_type=candidate.get('taskType'), timeout_rounds=candidate.get('timeoutRounds'),
                      solve_estimate=candidate.get('solveEstimate'), solve_source=candidate.get('solveSource'))
            elif reason in ('no_return_from_stand', 'no_path_to_weapon', 'no_path_to_station',
                            'no_station_or_weapon', 'return_zero_unexplained', 'no_threat_eta'):
                trace(state, pioneer.id, 'task_not_enough_time', '领取位置回炮未知或不可达，不接这单',
                      task_type=candidate.get('taskType'), return_reason=reason)
        mark_outcome(state, 'no_feasible_task', None, 'no_feasible_task')
        return False, None
    add_branch(state, 'commit_feasible_task')
    ok, cmd = apply_task_choice(pioneer, state, blocked, reserved, row)
    if ok:
        return True, cmd
    mark_outcome(state, 'task_move_blocked', None, 'task_move_blocked')
    return False, None


def _pioneer_shop_action(pioneer, state, blocked, reserved, reason='ordinary_shop'):
    add_branch(state, reason)
    stalled, stall_rounds = shop_progress_stalled(pioneer, state)
    if stalled:
        stall_reason = classify_shop_stall(pioneer, state)
        from .economy import pick_weapon_voucher_buyer
        buyer = pick_weapon_voucher_buyer(state, blocked)
        job = state.worker_item_jobs.get(pioneer.id)
        transferred = False
        if job and buyer is not None and buyer.id != pioneer.id:
            state.worker_item_jobs[buyer.id] = job
            del state.worker_item_jobs[pioneer.id]
            transferred = True
        trace(state, pioneer.id, 'shop_stall_reassess',
              '采购连续无进展，重新评估路线和买家，不默认取消必要购买',
              stallRounds=stall_rounds, threshold=SHOP_STALL_ROUNDS,
              stallReason=stall_reason, transferred=transferred,
              keepJob=bool(job) and not transferred)
        reset_shop_progress(state)
        if transferred:
            return None
    critical, crit_reason = voucher_is_defense_critical(state)
    reservation = reservation_of(state)
    allow = (not reservation) or critical
    cmd = decide_pioneer_voucher(
        pioneer, state, blocked, reserved,
        allow_interrupt=allow and bool(reservation),
        interrupt_reason=None if not reservation else ('defense_critical_voucher:%s' % crit_reason),
    )
    if cmd:
        mark_outcome(state, reason, cmd, 'shop')
        bump_streak(state, 'shop')
        return cmd
    return None


def decide_pioneer_day(pioneer: Role, state: "MatchState", blocked: set, reserved: set):
    """紧急治疗与真实防守优先；无强制防守时先评估自进化任务，再决定普通买券/卖矿。"""
    from .economy import in_pre_night_cashout_window, liquidate, muster_for_night
    from .tactics import tactical_action
    begin_schedule(state, pioneer, blocked, reserved)
    add_branch(state, 'emergency_heal')
    heal = decide_emergency_heal(pioneer, state)
    if heal:
        mark_outcome(state, 'emergency_heal', heal, 'emergency_heal')
        return selected(state, pioneer.id, heal, '低血紧急治疗')
    add_branch(state, 'task_before_ordinary_shop')
    handled, command = decide_pioneer_task(pioneer, state, blocked, reserved)
    if handled:
        return command
    add_branch(state, 'muster_for_night')
    handled, cmd = muster_for_night(pioneer, state, blocked, reserved)
    if handled:
        from .economy import defense_occupancy
        occupancy, snap = defense_occupancy(pioneer, state, blocked)
        if cmd:
            if has_task_reservation(state, pioneer):
                interrupt_reservation(state, 'defense_due', extra=snap)
            mark_outcome(state, 'muster_for_night', cmd, 'defense')
            bump_streak(state, 'defense')
            return cmd
        if occupancy == 'must_hold':
            mark_outcome(state, 'mandatory_hold', None, 'defense')
            bump_streak(state, 'defense')
            trace(state, pioneer.id, 'mandatory_hold_no_action',
                  '必须留守或操炮，本回合原地无移动', occupancy=occupancy,
                  defenseDueReasons=snap.get('defenseDueReasons'),
                  threatEta=snap.get('threatEta'), travel=snap.get('travel'))
            return None
        add_branch(state, 'muster_empty_continue_economy')
        trace(state, pioneer.id, 'muster_empty_continue_economy',
              'muster 未产生移动且无强制留守，继续评估买券等事务',
              occupancy=occupancy, defenseDue=snap.get('defenseDue'),
              defenseDueReasons=snap.get('defenseDueReasons'))
    if not has_task_reservation(state, pioneer):
        add_branch(state, 'pre_night_cashout')
        if in_pre_night_cashout_window(pioneer, state, blocked, reserved):
            handled, cmd = liquidate(pioneer, state, blocked, reserved)
            if cmd:
                mark_outcome(state, 'pre_night_cashout', cmd, 'shop')
                bump_streak(state, 'shop')
                return cmd
            cmd = _pioneer_shop_action(pioneer, state, blocked, reserved, 'pre_night_voucher')
            if cmd:
                return cmd
        cmd = _pioneer_shop_action(pioneer, state, blocked, reserved, 'ordinary_voucher')
        if cmd:
            return cmd
        handled, cmd = liquidate(pioneer, state, blocked, reserved)
        if handled:
            if cmd:
                mark_outcome(state, 'liquidate', cmd, 'shop')
                bump_streak(state, 'shop')
            return cmd
    else:
        add_branch(state, 'reservation_blocks_ordinary_shop')
        critical, crit_reason = voucher_is_defense_critical(state)
        if critical:
            cmd = _pioneer_shop_action(pioneer, state, blocked, reserved,
                                       'defense_critical_voucher:%s' % crit_reason)
            if cmd:
                return cmd
    cmd = tactical_action(pioneer, state, blocked, reserved)
    if cmd:
        mark_outcome(state, 'tactical', cmd, 'tactical')
        return cmd

    holds_weapon_voucher = held_weapon_voucher_target(pioneer, state, _pending_item_job_targets(state)) is not None
    if holds_weapon_voucher and not state.phase_task:
        maybe_start_shop_item_job(pioneer, state, allow_weapon=True, allow_structure_upgrade=False)
        cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
        if cmd:
            trace(state, pioneer.id, 'pioneer_uses_held_voucher', '开拓者手里有武器券，先用掉提升战力')
            mark_outcome(state, 'item_job', cmd, 'shop')
            return cmd
    item_job_cmd = None if self_evolution_work_open(state) else decide_shop_item_job(pioneer, state, blocked, reserved)
    if item_job_cmd and not has_task_reservation(state, pioneer):
        mark_outcome(state, 'item_job', item_job_cmd, 'shop')
        bump_streak(state, 'shop')
        return item_job_cmd

    if not has_task_reservation(state, pioneer) and not self_evolution_work_open(state):
        shop = find_zone(state, 'weaponShop')
        worker_at_shop = bool(shop and any(
            r.role_type == 'worker' and r.health > 0 and chebyshev(r.pos, shop.pos) <= 1
            for r in state.team_our.roles
        ))
        if not worker_at_shop:
            maybe_start_shop_item_job(pioneer, state, allow_weapon=False)
        cmd = decide_shop_item_job(pioneer, state, blocked, reserved)
        if cmd:
            mark_outcome(state, 'structure_shop', cmd, 'shop')
            bump_streak(state, 'shop')
            return cmd
    heal = decide_self_heal(pioneer) or decide_buy_medicine(pioneer, state)
    if heal:
        mark_outcome(state, 'self_heal', heal, 'heal')
        return heal
    from .opening import pioneer_stay_clear
    cmd = pioneer_stay_clear(pioneer, state, blocked, reserved)
    if cmd is None:
        mark_outcome(state, 'no_pioneer_action', None, 'idle')
        trace(state, pioneer.id, "no_pioneer_action", "当前没有可用任务，也未产生治疗、补给或维修升级动作", available_gold=state.team_our.gold_num)
    else:
        mark_outcome(state, 'stay_clear', cmd, 'stay_clear')
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
                from .treasure import shop_buy_allowed
                from .tactics import front_breached, pressure
                if not shop_buy_allowed(cmd["name"], state, emergency=pressure(state) or front_breached(state)):
                    trace(state, role.id, "early_buy_blocked", "第四天前拦截任务用品/召唤令/基地券购买", item=cmd["name"])
                    continue
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


def try_station_upgrade_during_cooldown(role: Role, state: "MatchState", blocked: set, reserved: set,
                                        weapon: Optional[Role] = None):
    """火箭冷却或射程内无目标时：若基地挨打且身上有 1→2 券，去用券回满血。"""
    if "StationUpgradeVoucher1" not in role.backpack:
        return None
    station = own_station(state)
    if not station or (station.level or 1) >= 2:
        return None
    if not station_voucher_use_now(state):
        return None
    from .tactics import threat_robots
    if weapon is not None:
        ready = weapon.role_type != "rocket" or (weapon.cooldown or 0) == 0
        if ready and plan_attack(weapon, threat_robots(state), state):
            return None
        cooldown = 99 if weapon.role_type != "rocket" else (weapon.cooldown or 0)
    else:
        cooldown = 99
    from .opening import adjacent_path, mobile_walkable, move_on_path
    walkable = mobile_walkable(state, blocked, reserved)
    if chebyshev(role.pos, station.pos) <= 1:
        job = state.worker_item_jobs.get(role.id)
        if job and job.get("kind") == "station":
            job["awaiting_use"] = True
        return selected(
            state, role.id,
            {"action": "use", "name": "StationUpgradeVoucher1",
             "targetPos": [{"x": station.pos.x, "y": station.pos.y}]},
            "火箭冷却，基地挨打时升2级回满血",
        )
    path = adjacent_path(role, station.pos, walkable, state)
    if path is None:
        return None
    back = len(path)
    if weapon is not None and cooldown < len(path) + 1 + back:
        trace(state, role.id, "station_upgrade_wait_cooldown",
              "冷却不够走去用基地券再回炮，留在炮位", cooldown=cooldown, travel=len(path))
        return None
    return move_on_path(state, role, path, reserved, "火箭冷却，前往基地使用升级券")


def adjacent_ready_rocket(fighter: Role, assigned: Optional[Role], state: "MatchState", robots: list,
                          commands: dict, ledger: Optional[DamageLedger] = None,
                          ctx: Optional[TargetContext] = None):
    """火箭冷却时，同一操作者可切到相邻且已冷却的另一门火箭炮；返回 (武器, (落点, 伤害表))。"""
    from .opening import dual_rocket_partner
    used_weapons = set(commands)
    partner = None
    if assigned is not None:
        blocked = build_blocked_set(state) - {(fighter.pos.x, fighter.pos.y)}
        partner, _stands = dual_rocket_partner(state, assigned, blocked)
    candidates = []
    for weapon in state.team_our.roles:
        if weapon.role_type != "rocket" or weapon.health <= 0 or weapon.id in used_weapons:
            continue
        if assigned is not None and weapon.id == assigned.id:
            continue
        if partner is not None and weapon.id != partner.id:
            continue
        if chebyshev(fighter.pos, weapon.pos) > 1:
            continue
        if weapon.cooldown or 0:
            continue
        plan = plan_attack(weapon, robots, state, ledger, ctx)
        if plan:
            candidates.append((weapon.level or 1, -weapon.id, weapon, plan))
    if not candidates:
        return None, None
    _, _, weapon, plan = max(candidates, key=lambda c: c[:2])
    return weapon, plan


def plan_pioneer_tasks(state, blocked, reserved):
    """先规划先锋任务；接管的先锋不再参与开局或武器分配。"""
    commands, handled_ids = {}, set()
    for role in state.team_our.roles:
        if role.role_type != "pioneer":
            continue
        begin_schedule(state, role, blocked, reserved)
        handled, command = decide_pioneer_task(role, state, blocked, reserved)
        if handled:
            handled_ids.add(role.id)
            trace(state, role.id, "pioneer_task", "白天安全时间内执行任务，回防时让出角色")
            if command:
                commands[role.id] = command
    return commands, handled_ids


def _night_worker_release(state, blocked, reserved):
    """两人三炮下第三个人（一名工人）的夜间安排，返回 (工人, 指令)；不放人返回 (None, None)。
    不看任务点是否可接：未清波时开拓者不接新任务、留在守炮名单。
    开拓者在做天黑前已开始的任务时由 plan_night 直接不调用本函数。
    - 前两夜：优先放经济工去后院采矿，施工工和开拓者守三炮。
    - 第三夜起：优先放施工工，经济工一人守双火箭、开拓者开另一门。
    - 偏好的人出不去（被炮位夹角堵住）或没事可做时，换另一名工人。
    - 敌人逼近/有压力时叫人回防：前两夜一律不叫（火箭 3 回合冷却，空手回来改变不了什么），
      只要剩下两人结构上能覆盖三炮就继续在外面干活；第三夜起只有外出的人身上带着
      升级券、修墙道具或战斗道具时才按压力叫回，回来先在家用道具给残墙回血（夜里不能建造）。
    - 只放站在安全处（不在正面、机器人及其进攻路线上）的人；外出只走完全避险的路线。"""
    later_night = structure_priority_day(state)
    workers = [r for r in state.team_our.roles if r.role_type == "worker" and r.health > 0]
    if len(workers) < 2:
        return None, None
    from .opening import carries_home_defense_item, guns_covered_without, night_danger_cells
    from .opening_schedule import opening_worker_mode
    from .tactics import front_breached, pressure
    wanted = "builder" if later_night else "economist"
    pressed = pressure(state) or front_breached(state)
    # 按分工偏好排序，但不死认一个人：偏好的人被堵在炮位夹角里出不去、没矿可去时，换另一名工人出去。
    # 上一回合放出去的人优先继续外出，避免两名工人来回换班。
    previous = state.policy_memory.get("night_released_worker")
    candidates = sorted(workers, key=lambda w: (w.id != previous,
                                                opening_worker_mode(state, w) != wanted,
                                                -len(w.backpack or []), -w.id))
    if not later_night:
        # 前两夜施工工必须留家开双火箭，只放经济工外出。
        stay_home = [w for w in candidates if opening_worker_mode(state, w) != "builder"]
        if stay_home:
            candidates = stay_home
    released = cmd = reason = None
    danger = night_danger_cells(state)
    for worker in candidates:
        if (worker.pos.x, worker.pos.y) in danger:
            # 还站在正面/机器人进攻路线上：先按守炮的人沿避险路线撤回基地，撤到安全处再放出去。
            trace(state, worker.id, "night_worker_release_unsafe", "人还在危险区，先撤回基地再外出")
            continue
        recall_on_pressure = later_night and carries_home_defense_item(worker)
        under_pressure = recall_on_pressure and pressed
        if under_pressure:
            covered = guns_covered_without(state, {worker.id}, blocked, max_travel=NIGHT_REPAIR_GUNNER_TRAVEL)
        else:
            covered = guns_covered_without(state, {worker.id}, blocked, enemy_timing=recall_on_pressure)
        if not covered:
            trace(state, worker.id, "night_worker_release_uncovered", "放这名工人后剩下两人守不住三炮")
            continue
        state.night_released_ids = {worker.id}
        try:
            if not later_night:
                cmd = decide_worker_day(worker, state, blocked, reserved)
                reason = "前两夜两人三炮：一名工人去后院采矿，另一名工人与开拓者守炮"
            else:
                # 有压力且带着道具：先在家修残墙；否则照常去后院采矿。
                cmd = maintain_front_wall_health(worker, state, blocked, reserved) if under_pressure else None
                reason = "第三夜起有压力且带着道具：两人守三炮，另一名工人在家用道具修残墙（夜里不建造）"
                if not cmd:
                    cmd = decide_worker_day(worker, state, blocked, reserved)
                    reason = "第三夜起：两人守三炮，另一名工人先修残墙再去后院采矿"
        finally:
            state.night_released_ids = set()
        if cmd:
            released = worker
            break
        trace(state, worker.id, "night_worker_release_idle", "放出去也没事可做（无可达的后院矿等），换人或留守")
    if released is None:
        state.policy_memory.pop("night_released_worker", None)
        return None, None
    state.policy_memory["night_released_worker"] = released.id
    trace(state, released.id, "night_worker_released_to_economy", reason, command=cmd)
    return released, cmd


def night_voucher_use(fighter: Role, state: "MatchState", target):
    """夜里守炮的人手里有券：身边有同级武器，且这回合没有可打的目标（冷却或射程外）就地升级。"""
    if target is not None:
        return None
    for name in ("WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2"):
        if name not in (fighter.backpack or []):
            continue
        level = 1 if name.endswith("1") else 2
        near = [r for r in state.team_our.roles
                if r.role_type in WEAPON_TYPES and r.health > 0 and (r.level or 1) == level
                and chebyshev(fighter.pos, r.pos) <= 1]
        if near:
            weapon = min(near, key=lambda r: (_WEAPON_UPGRADE_ORDER.get(r.role_type, 99), r.id))
            return selected(state, fighter.id, {"action": "use", "name": name,
                                                "targetPos": [{"x": weapon.pos.x, "y": weapon.pos.y}]},
                            "夜里手里有武器券，趁炮冷却/无目标就地升级")
    return None


def plan_night(state: "MatchState") -> dict:
    from .opening import assign_weapons, move_on_path, weapon_approach_path, _fighter_layer
    from .tactics import (
        tactical_action, night_wave_cleared, night_near_work_allowed,
        threat_robots, pressure, front_breached,
    )
    commands = {}
    if not state.team_our or not state.map_info:
        return commands
    state = copy(state)
    state.team_our = copy(state.team_our)
    blocked, reserved = build_blocked_set(state), set()
    robots = threat_robots(state)
    if night_wave_cleared(state):
        trace(state, None, 'night_wave_cleared', '连续空窗达到试探外出条件，不是官方清波；转为任务、采矿和修墙')
        for role in state.team_our.roles:
            if role.role_type == 'worker':
                cmd = decide_worker_day(role, state, blocked, reserved)
            elif role.role_type == 'pioneer':
                cmd = decide_pioneer_day(role, state, blocked, reserved)
            else:
                continue
            if cmd:
                if cmd.get('action') == 'buy':
                    from .treasure import shop_buy_allowed
                    from .tactics import front_breached, pressure
                    if not shop_buy_allowed(cmd['name'], state, emergency=pressure(state) or front_breached(state)):
                        trace(state, role.id, 'early_buy_blocked', '第四天前拦截任务用品/召唤令/基地券购买', item=cmd['name'])
                        continue
                    cost = item_cost(cmd['name'], state) * cmd.get('num', 1)
                    if cost > state.team_our.gold_num:
                        continue
                    state.team_our.gold_num -= cost
                commands[role.id] = cmd
        return commands
    # 机器人未清完前固定"两人三炮 + 一名工人去后院采矿"：开拓者不接新任务，留在守炮名单。
    # 唯一例外：天黑前已开始的任务（离开任务点即失败）做完再回炮，这期间两名工人守炮、不放人。
    # 白天留下的 approaching 预约不再去接，直接清掉，避免预约悬空；清波后开拓者才恢复接任务。
    from .pioneer_schedule import clear_reservation, has_task_reservation
    commands, task_pioneers = {}, set()
    pioneer = next((r for r in state.team_our.roles
                    if r.role_type == 'pioneer' and r.health > 0), None)
    if not state.phase_task and pioneer and has_task_reservation(state, pioneer):
        clear_reservation(state, 'night_defense_clears_approaching')
        trace(state, pioneer.id, "night_clear_reservation",
              "未清波：开拓者守炮，清掉白天留下的任务预约")
    if state.phase_task:
        commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
        trace(state, None, "night_hold_active_task",
              "开拓者做完已开始的任务再回炮；两名工人守三炮，暂不放人采矿")
    else:
        trace(state, None, "night_fixed_defense",
              "未清波：两人三炮，一名工人去后院采矿；开拓者留守不接任务")
    urgent = pressure(state) or front_breached(state)
    released_worker, released_cmd = (None, None) if task_pioneers else _night_worker_release(state, blocked, reserved)
    excluded = set(task_pioneers)
    exit_hold = set()
    if released_worker is not None:
        excluded.add(released_worker.id)
        commands[released_worker.id] = released_cmd
        # 外出的人还在院里：把他出院要经过的格留给他，守炮的人本回合不先站上去把他堵住。
        # 只限制本回合的落脚格，不当成整条路线的障碍（远处的人照常规划回炮路线）。
        from .opening import yard_exit_cells
        exit_hold = yard_exit_cells(released_worker, state, blocked)
    assignments = assign_weapons(state, excluded_ids=excluded, persist=True)
    fighters = [r for r in state.team_our.roles
                if r.role_type in ("worker", "pioneer") and r.health > 0 and r.id not in excluded]
    # 已在炮位的先决策；其中火箭先算，电磁炮读同一份伤害表补刀。
    fighters.sort(key=lambda r: (
        0 if (assignments.get(r.id) and chebyshev(r.pos, assignments[r.id].pos) <= 1) else 1,
        0 if (assignments.get(r.id) and assignments[r.id].role_type == "rocket") else 1,
        _fighter_layer(state, r),
    ))
    target_ctx, ledger = TargetContext(state, robots), DamageLedger()
    for fighter in fighters:
        heal = decide_emergency_heal(fighter, state)
        if heal:
            commands[fighter.id] = selected(state, fighter.id, heal, '低血紧急治疗')
            continue
        if urgent:
            cmd = tactical_action(fighter, state, blocked, reserved, allow_travel=False)
            if cmd:
                if cmd['action'] == 'buy':
                    from .treasure import shop_buy_allowed
                    if not shop_buy_allowed(cmd['name'], state, emergency=True):
                        continue
                    state.team_our.gold_num -= item_cost(cmd['name'], state) * cmd.get('num', 1)
                commands[fighter.id] = cmd
                continue
        weapon = assignments.get(fighter.id)
        at_gun = weapon is not None and chebyshev(fighter.pos, weapon.pos) <= 1
        if weapon is not None:
            ready = weapon.role_type != "rocket" or (weapon.cooldown or 0) == 0
            plan = plan_attack(weapon, robots, state, ledger, target_ctx) if (at_gun and ready) else None
            if at_gun:
                upgrade = night_voucher_use(fighter, state, plan)
                if upgrade:
                    commands[fighter.id] = upgrade
                    continue
            if plan is None and weapon.role_type == "rocket":
                alternate, alt_plan = adjacent_ready_rocket(fighter, weapon, state, robots, commands,
                                                            ledger, target_ctx)
                if alternate is not None:
                    trace(state, fighter.id, "weapon_assignment",
                          "分配火箭冷却，切到相邻已冷却火箭炮开火", weapon_id=alternate.id,
                          assigned_weapon_id=weapon.id)
                    trace(state, fighter.id, "selected", TARGETING_REASON,
                          weapon_id=alternate.id, target_pos=alt_plan[0], expected_damage=alt_plan[1])
                    ledger.add(alt_plan[1])
                    commands[alternate.id] = {
                        "action": "attack", "controllerId": str(fighter.id), "targetPos": alt_plan[0],
                    }
                    continue
                from .opening import control_stand_path
                stand_path = control_stand_path(fighter, weapon, blocked, reserved | exit_hold, state)
                need_stand = (weapon.cooldown or 0) > 0
                if need_stand and stand_path:
                    cmd = move_on_path(state, fighter, stand_path, reserved,
                                       '分配火箭暂时打不了，先站到双火箭共用操控位轮流开火')
                    if cmd:
                        commands[fighter.id] = cmd
                        continue
                if need_stand and stand_path == [] and not at_gun:
                    trace(state, fighter.id, "weapon_assignment", "两人三炮分配；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
                    trace(state, fighter.id, "weapon_cooldown", "火箭冷却，留在另一门火箭旁轮流开火", weapon_id=weapon.id)
                    continue
            if at_gun:
                if plan:
                    trace(state, fighter.id, "weapon_assignment", "两人三炮分配；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
                    trace(state, fighter.id, "selected", TARGETING_REASON,
                          weapon_id=weapon.id, target_pos=plan[0], expected_damage=plan[1])
                    ledger.add(plan[1])
                    commands[weapon.id] = {"action": "attack", "controllerId": str(fighter.id),
                                           "targetPos": plan[0]}
                    continue
                heal_cmd = try_station_upgrade_during_cooldown(fighter, state, blocked, reserved, weapon)
                if heal_cmd:
                    commands[fighter.id] = heal_cmd
                    continue
                trace(state, fighter.id, "weapon_assignment", "两人三炮分配；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
                if fighter.role_type == 'worker' and night_near_work_allowed(state) and not robots:
                    from .opening import adjacent_critical_build
                    near = adjacent_critical_build(fighter, state, blocked, reserved)
                    if near:
                        commands[fighter.id] = near
                        continue
                    cmd = tactical_action(fighter, state, blocked, reserved, allow_travel=False)
                    if cmd:
                        if cmd['action'] == 'buy':
                            from .treasure import shop_buy_allowed
                            if not shop_buy_allowed(cmd['name'], state, emergency=urgent):
                                continue
                            state.team_our.gold_num -= item_cost(cmd['name'], state) * cmd.get('num', 1)
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
        trace(state, fighter.id, "weapon_assignment", "两人三炮分配；里侧开里炮、外侧开外炮", weapon_id=weapon.id)
        from .opening import night_danger_cells
        danger = night_danger_cells(state, include_front=False) - {(fighter.pos.x, fighter.pos.y)}
        # 队友本回合的落脚格和外出的人的出院通道都是临时占位：先绕开它们找路，
        # 找不到就不把它们当整条路线的障碍，只是第一步不踩上去（踩上去就原地等一回合）。
        path = None
        for avoid in (blocked | danger, blocked):
            path = weapon_approach_path(fighter, weapon, avoid, reserved | exit_hold, state)
            if path is None:
                path = weapon_approach_path(fighter, weapon, avoid, set(), state)
            if path is not None:
                break
        if path and (path[0].x, path[0].y) in (reserved | exit_hold):
            trace(state, fighter.id, "night_wait_for_teammate", "回炮第一步被队友本回合的落脚格占着，先让一回合")
            continue
        cmd = move_on_path(state, fighter, path, reserved, "前往独立分配的武器（绕开机器人进攻路线）")
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


def command_actor_id(key, command):
    if command.get('action') == 'attack':
        try:
            return int(command.get('controllerId'))
        except (TypeError, ValueError):
            return None
    return key


def worker_defers_voucher_use(role: Role, state: "MatchState", blocked: set) -> bool:
    """工人白天拿到武器券先不专程去用：背包还有空、也没到回防时间，就继续采矿修墙，
    回防时顺路到武器旁用掉（夜里守炮的人没目标时也会就地用）。开拓者夜里要开电磁炮，拿到就用。"""
    if role.role_type != "worker" or not is_day_round(state.round_no):
        return False
    cap = role.back_pack_capability or 0
    if cap and len(role.backpack or []) >= cap:
        return False
    from .economy import defense_due
    return not defense_due(role, state, blocked)


def enforce_held_vouchers(state: "MatchState", commands: dict) -> dict:
    """白天兜底：手里有武器升级券的开拓者本回合就去用；工人身边就有对应武器时当场用，
    要专门走过去的等背包满了或到了回防时间才去（顺路回家）。
    目标由 held_weapon_voucher_target 决定（顺序优先、否则任意同级），同一回合两人不升同一门。
    不接管：紧急治疗、正在买武器券（批量买完再去）、开拓者任务进行中、没有同级武器、工人暂缓使用。"""
    if not state.team_our or not state.map_info or not is_day_round(state.round_no):
        return commands
    from .opening import adjacent_path, move_on_path
    blocked = build_blocked_set(state)
    taken = set()
    for role in state.team_our.roles:
        if role.role_type not in ("worker", "pioneer") or role.health <= 0:
            continue
        if role.role_type == "pioneer" and state.phase_task:
            continue  # 任务进行中离开任务点即失败
        current = commands.get(role.id) or {}
        if current.get("action") == "use" and current.get("name") == "Medicine":
            continue
        if current.get("action") == "buy" and str(current.get("name", "")).startswith("WeaponUpgradeVoucher"):
            continue
        pick = held_weapon_voucher_target(role, state, taken)
        if pick is None:
            continue
        name, weapon = pick
        if chebyshev(role.pos, weapon.pos) > 1 and worker_defers_voucher_use(role, state, blocked):
            continue  # 工人要专门走过去才能用：先干活，回防时顺路用
        target = (weapon.pos.x, weapon.pos.y)
        taken.add(target)
        if current.get("action") == "use" and current.get("name") == name:
            continue
        state.worker_item_jobs[role.id] = {"item": name, "target": target, "kind": "weapon"}
        if chebyshev(role.pos, weapon.pos) <= 1:
            state.worker_item_jobs[role.id]["awaiting_use"] = True
            cmd = selected(state, role.id, {"action": "use", "name": name,
                                            "targetPos": [{"x": weapon.pos.x, "y": weapon.pos.y}]},
                           "手里有武器券，立即使用")
        else:
            reserved = {(c["targetPos"][0]["x"], c["targetPos"][0]["y"])
                        for rid, c in commands.items()
                        if rid != role.id and c.get("action") == "move" and c.get("targetPos")}
            path = adjacent_path(role, weapon.pos, (blocked | reserved) - {(role.pos.x, role.pos.y)}, state)
            if not path:
                trace(state, role.id, "held_voucher_unreachable", "持券：目标武器暂时不可达",
                      item=name, weapon_id=weapon.id)
                continue
            cmd = move_on_path(state, role, path, set(), "手里有武器券，走去武器旁使用")
        if not cmd:
            continue
        trace(state, role.id, "held_voucher_use", "手里有武器券就用：顺序优先，否则任意同级武器",
              item=name, weapon_id=weapon.id, weapon_level=weapon.level or 1, overridden=current or None)
        commands[role.id] = cmd
    return commands


def resolve_actor_conflicts(commands: dict, state: "MatchState") -> dict:
    """同一执行角色只能有一条指令：紧急治疗覆盖其操炮及其它动作。计划阶段的预算副本会丢弃，不改活状态金币。"""
    if not commands or not state.team_our:
        return commands
    roles = {r.id: r for r in state.team_our.roles}
    healers = set()
    for key, command in commands.items():
        role = roles.get(key)
        if (command.get('action') == 'use' and command.get('name') == 'Medicine'
                and role and decide_emergency_heal(role, state)):
            healers.add(role.id)
    kept = {}
    for key, command in commands.items():
        actor = command_actor_id(key, command)
        if actor in healers and not (
                command.get('action') == 'use' and command.get('name') == 'Medicine' and key == actor):
            trace(state, actor, 'emergency_heal_preempts', '低血紧急治疗覆盖该角色的操炮或其它动作', dropped=command)
            continue
        kept[key] = command
    return kept


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
        sched, _intent = ensure_schedule_buckets(state)
        sched.clear()  # 调度上下文只在本回合有效
        from .tactics import begin_round
        begin_round(state)
        learn_from_last_round(state)
        log_judge_feedback(state)
        if not state.team_our or not state.map_info:
            trace(state, None, "missing_state", "缺少队伍或地图快照，不能生成指令")
            commands = {}
        elif isinstance(state.round_no, int) and own_station(state):
            from .opening import plan_opening
            if 0 <= state.round_no < DAY_ROUNDS:
                commands = plan_opening(state)
            elif is_day_round(state.round_no):
                commands = plan_day(state)
            else:
                commands = plan_night(state)
        elif is_day_round(state.round_no):
            commands = plan_day(state)
        else:
            commands = plan_night(state)
        commands = enforce_held_vouchers(state, commands)
        # 最低优先级兜底，不能同时占用正在操炮的角色。
        if state.team_our:
            controllers = {str(c.get('controllerId')) for c in commands.values()}
            for role in state.team_our.roles:
                if role.role_type in ('worker', 'pioneer') and role.health > 0 and role.id not in commands and str(role.id) not in controllers:
                    heal = decide_self_heal(role)
                    if heal:
                        commands[role.id] = selected(state, role.id, heal, '所有更高优先级分支均无行动，最后自救')
        commands = resolve_actor_conflicts(commands, state)
        commands = self._filter_valid(commands, state)
        state.last_sent_command = commands
        emit_scheduler_log(state, commands)
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
