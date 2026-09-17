"""全局工作单与统一截止时间。

本模块只做计算与记账，不产生任何指令。角色状态机从这里取目标、批量大小和截止回合，
避免每个角色各自按局部优先级临时决策。每回合计算一次并缓存在 state 上。

工作单四项：emergency_defense / weapon_minimum / first_upgrade / wall_program。
"""
from .decision_log import trace
from .grid import chebyshev

# 接口文档 1.1：backpack 是 String[]，物品不堆叠，一个石头占一格、修一段墙。
STONE_PER_WALL = 1
# 第一批关键墙的目标段数。这是"先把关键入口封住"的批次大小，不是当天墙数上限。
FIRST_BATCH_WALLS = 8
# 单段墙的工时估计：建造 1 回合 + 走位 1 回合。不是官方耗时。
ROUNDS_PER_WALL = 2
ROUNDS_PER_STONE = 1
WALL_VALUE = 3.0
STONE_VALUE = 1.0
IDLE_PENALTY = 0.2
RISK_PENALTY = 2.0
WORK_ORDERS_KEY = '_work_orders'
WALL_PROGRAM_KEY = 'wall_program'
BUILDER_STATE_KEY = 'builder_state'

BUILDER_STATES = (
    'BUILD_MINIMUM_WEAPONS', 'PLAN_STONE_BATCH', 'GO_STONE', 'GATHER_STONE_BATCH',
    'GO_WALL_LINE', 'BUILD_WALL_BATCH', 'PLAN_NEXT_ACTION', 'RETURN_DEFENSE',
)


# ---------------------------------------------------------------- 背包（全部读快照）

def inventory_capacity(role):
    """背包总格数，来自快照 backPackCapability。未知时返回 None，绝不猜测上限。"""
    cap = getattr(role, 'back_pack_capability', 0) or 0
    return cap if cap > 0 else None


def inventory_used(role):
    return len(role.backpack or [])


def free_slots(role):
    cap = inventory_capacity(role)
    if cap is None:
        return None
    return max(0, cap - inventory_used(role))


def backpack_full(role):
    free = free_slots(role)
    return free is not None and free <= 0


def stone_count(role):
    return (role.backpack or []).count('stone')


def stone_needed_for(wall_slots):
    return max(0, int(wall_slots)) * STONE_PER_WALL


def collect_failed_last_round(state, role):
    """上回合 collect 被判失败：背包满或矿已耗尽，都要离开石矿。"""
    prev = (state.last_sent_command or {}).get(role.id) or {}
    if prev.get('action') != 'collect':
        return False
    return (state.last_round_role_action_results or {}).get(role.id) is False


# ---------------------------------------------------------------- 统一截止时间

def _travel_home(state, role, blocked):
    from .opening import station_return_steps
    steps = station_return_steps(role, state, blocked)
    return steps


def return_deadline(state, role, blocked, safety_margin=None):
    """该角色必须停止一切非防守操作、开始回防的硬截止回合（白天 cycle 口径）。

    return_deadline = 入夜回合 - 到炮位最短路 - 安全余量。回炮路不可达时返回 None。
    """
    from .brain import DAY_ROUNDS
    from .opening import MUSTER_BUFFER
    if safety_margin is None:
        safety_margin = MUSTER_BUFFER
    travel = _travel_home(state, role, blocked)
    if travel is None:
        return None
    return DAY_ROUNDS - travel - safety_margin


def rounds_before_return(state, role, blocked, safety_margin=None):
    """距离本角色 return_deadline 还剩几回合；不可达返回 0（立即回防）。"""
    from .brain import DAY_NIGHT_CYCLE
    deadline = return_deadline(state, role, blocked, safety_margin)
    if deadline is None:
        return 0
    cycle = (state.round_no or 0) % DAY_NIGHT_CYCLE
    return max(0, deadline - cycle)


def wall_deadline(state, role, blocked):
    """完成当前墙批并安全回炮的最晚回合。与 return_deadline 同源，单列便于日志。"""
    return return_deadline(state, role, blocked)


def upgrade_deadline(state, role, blocked):
    """卖矿 + 买券 + 用券 + 回防的最晚启动回合。"""
    from .brain import DAY_ROUNDS, find_zone
    from .opening import MUSTER_BUFFER, adjacent_path
    travel = _travel_home(state, role, blocked)
    if travel is None:
        return None
    chain = 0
    for neutral in ('vendor', 'weaponShop'):
        zone = find_zone(state, neutral)
        if zone is None:
            continue
        path = adjacent_path(role, zone.pos, blocked, state)
        if path is None:
            return None
        chain += len(path) + 1
    return DAY_ROUNDS - travel - chain - MUSTER_BUFFER


def task_deadline(state, role, blocked):
    """开拓者完成任务并安全回防的最晚回合。"""
    return return_deadline(state, role, blocked)


def fits_before_deadline(state, role, blocked, action_eta, deadline=None):
    """统一校验：current_round + action_eta + return_to_defense_eta <= deadline。"""
    from .brain import DAY_NIGHT_CYCLE
    if deadline is None:
        deadline = return_deadline(state, role, blocked)
    if deadline is None:
        return False
    cycle = (state.round_no or 0) % DAY_NIGHT_CYCLE
    return cycle + max(0, int(action_eta)) <= deadline


# ---------------------------------------------------------------- 墙目标（动态，不写死段数）

def wall_minimum_target(state):
    """封住关键入口、保护基地和武器所需的最低墙位数。"""
    from .brain import own_station
    from .opening import survival_wall_plan
    base = own_station(state)
    if base is None:
        return 0
    return len(survival_wall_plan(state, base))


def _max_walls_before_deadline(state, blocked):
    """夜前还能完成多少段墙的上界估计。启发式工时，不是官方耗时。"""
    workers = [r for r in (state.team_our.roles if state.team_our else [])
               if r.role_type == 'worker' and r.health > 0]
    if not workers:
        return 0
    total = 0
    for worker in workers:
        rounds = rounds_before_return(state, worker, blocked)
        if rounds <= 0:
            continue
        # 手上已有的石头不用再跑矿；其余按 采一块 + 建一段 摊算。
        have = stone_count(worker)
        from_hand = min(have, rounds // ROUNDS_PER_WALL)
        left = max(0, rounds - from_hand * ROUNDS_PER_WALL)
        total += from_hand + left // (ROUNDS_PER_WALL + ROUNDS_PER_STONE)
    return total


def wall_feasible_target(state, blocked=None):
    """第一天/第二天的墙目标：地图墙位、当前防线规模、夜前可完成量取小。

    绝不用 8/10/12 之类的常数截断——这些只能是批次大小。
    """
    from .brain import own_station
    from .grid import build_blocked_set
    from .opening import primary_wall_plan
    base = own_station(state)
    if base is None:
        return 0
    cached = getattr(state, '_wall_feasible_cache', None)
    if isinstance(cached, tuple) and cached[0] == state.round_no:
        return cached[1]
    if blocked is None:
        blocked = build_blocked_set(state)
    plan_slots = len(primary_wall_plan(state, base))
    built_on_plan = sum(1 for p in primary_wall_plan(state, base)
                        if any(r.role_type == 'wall' and r.health > 0 and (r.pos.x, r.pos.y) == p
                               for r in state.team_our.roles))
    minimum = wall_minimum_target(state)
    reachable = built_on_plan + _max_walls_before_deadline(state, blocked)
    value = int(min(plan_slots, max(minimum, reachable)))
    try:
        state._wall_feasible_cache = (state.round_no, value)
    except AttributeError:
        pass
    return value


def remaining_wall_slots(state):
    """按优先级排序的、当前仍缺的墙位：关键缺口在前。"""
    from .brain import own_station
    from .opening import primary_wall_plan, survival_wall_plan, wall_priority
    base = own_station(state)
    if base is None:
        return []
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles
                if r.role_type == 'wall' and r.health > 0}
    critical = [p for p in survival_wall_plan(state, base) if p not in existing]
    rest = [p for p in primary_wall_plan(state, base)
            if p not in existing and p not in critical]
    rest.sort(key=lambda p: (wall_priority(state, base, p), p))
    return critical + rest


# ---------------------------------------------------------------- 工作单 A：紧急防守

def emergency_defense(state):
    """只有真实危险才算紧急。普通"墙还没修够"不是紧急，不能长期抢占经济与升级。"""
    from .brain import (
        EMERGENCY_HP_ABS, max_health, own_station, station_under_attack, wall_about_to_fall,
    )
    from .tactics import front_breached, imminent_contact, threat_robots
    reasons = []
    if imminent_contact(state):
        reasons.append('enemy_imminent')
    if front_breached(state):
        reasons.append('front_breached')
    if station_under_attack(state):
        reasons.append('station_under_attack')
    robots = threat_robots(state)
    for role in (state.team_our.roles if state.team_our else []):
        if role.role_type in ('gatling', 'railgun', 'rocket') and role.health > 0:
            if any(chebyshev(role.pos, r.pos) <= 2 for r in robots):
                reasons.append('weapon_under_attack')
                break
    for role in (state.team_our.roles if state.team_our else []):
        if role.role_type == 'wall' and wall_about_to_fall(role, state):
            reasons.append('wall_breaking')
            break
    for role in (state.team_our.roles if state.team_our else []):
        if role.role_type in ('worker', 'pioneer') and 0 < role.health <= max(
                EMERGENCY_HP_ABS, max_health(role) * 0.15):
            reasons.append('role_critical_hp')
            break
    station = own_station(state)
    if station is not None and station.health > 0 and station.health < max_health(station) * 0.5:
        reasons.append('station_low_hp')
    return {'active': bool(reasons), 'reasons': sorted(set(reasons))}


# ---------------------------------------------------------------- 工作单 B：三门基础武器

def weapon_minimum(state):
    from .brain import MAX_WEAPONS, WEAPON_TYPES
    built = sum(1 for r in (state.team_our.roles if state.team_our else [])
                if r.role_type in WEAPON_TYPES and r.health > 0)
    return {'built': built, 'target': MAX_WEAPONS, 'complete': built >= MAX_WEAPONS}


# ---------------------------------------------------------------- 工作单 C：第一张升级券

STAGE_FUND = 'fund'
STAGE_SELL = 'sell'
STAGE_BUY = 'buy_voucher'
STAGE_APPLY = 'apply_voucher'
STAGE_COMPLETE = 'complete'


def first_upgrade(state, blocked=None):
    """第一张武器升级券的显式闭环。已有人持券时，其他人不得为同一次升级重复买券。"""
    from .brain import WEAPON_TYPES, _pending_item_job_targets, _pick_upgradeable, item_cost
    from .economy import team_metal_inventory_value
    from .opening import live_l2_weapon_count
    cost = item_cost('WeaponUpgradeVoucher1', state)
    gold = state.team_our.gold_num if state.team_our else 0
    roles = [r for r in (state.team_our.roles if state.team_our else [])
             if r.role_type in ('worker', 'pioneer') and r.health > 0]
    holder = next((r for r in roles if 'WeaponUpgradeVoucher1' in (r.backpack or [])), None)
    job_owner = next((rid for rid, job in (state.worker_item_jobs or {}).items()
                      if job.get('kind') == 'weapon'), None)
    weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state),
                               max_current_level=1)
    order = {
        'upgrade_target_weapon': None if weapon is None else weapon.id,
        'target_pos': None if weapon is None else [weapon.pos.x, weapon.pos.y],
        'upgrade_owner': None,
        'funding_gap': max(0, cost - gold),
        'cost': cost,
        'gold': gold,
        'inventory_value': team_metal_inventory_value(state),
        'upgrade_stage': STAGE_FUND,
    }
    if live_l2_weapon_count(state) >= 1:
        order['upgrade_stage'] = STAGE_COMPLETE
        order['funding_gap'] = 0
        return order
    if holder is not None:
        order.update(upgrade_stage=STAGE_APPLY, upgrade_owner=holder.id, funding_gap=0)
        return order
    if job_owner is not None:
        order['upgrade_owner'] = job_owner
    if gold >= cost:
        order['upgrade_stage'] = STAGE_BUY
        if order['upgrade_owner'] is None:
            from .opening_schedule import _voucher_buyer_id
            order['upgrade_owner'] = _voucher_buyer_id(state, gold, cost)
        return order
    if gold + order['inventory_value'] >= cost:
        order['upgrade_stage'] = STAGE_SELL
        return order
    return order


# ---------------------------------------------------------------- 工作单 D：城墙工程

def wall_program(state, blocked):
    """可持续、可交接的全局城墙工作单。建造工死亡时工作单不消失，只换 owner。"""
    from .brain import DAY_NIGHT_CYCLE
    from .opening_schedule import opening_worker_roles
    memory = state.policy_memory.setdefault(WALL_PROGRAM_KEY, {})
    workers = {r.id: r for r in (state.team_our.roles if state.team_our else [])
               if r.role_type == 'worker' and r.health > 0}
    assigned = opening_worker_roles(state)
    builder = assigned.get('builder')
    helper = assigned.get('economist')

    previous_builder = memory.get('assigned_builder')
    takeover = None
    if previous_builder is not None and previous_builder not in workers:
        # 建造工阵亡或不可用：工作单交给经济工，不能连同任务一起消失。
        takeover = 'builder_unavailable'
        if helper in workers:
            builder, helper = helper, None
    elif previous_builder is not None and previous_builder != builder:
        takeover = 'builder_reassigned'

    slots = remaining_wall_slots(state)
    minimum = wall_minimum_target(state)
    feasible = wall_feasible_target(state, blocked)
    built = sum(1 for r in (state.team_our.roles if state.team_our else [])
                if r.role_type == 'wall' and r.health > 0)
    target_slots = slots[:max(0, feasible - built)] if feasible > built else []
    if not target_slots:
        target_slots = slots[:max(0, minimum - built)]
    stone_by_role = {rid: stone_count(role) for rid, role in workers.items()}
    owner = workers.get(builder)
    deadline = None if owner is None else wall_deadline(state, owner, blocked)
    required = stone_needed_for(len(target_slots))
    cycle = (state.round_no or 0) % DAY_NIGHT_CYCLE
    have = sum(stone_by_role.values())
    eta = len(target_slots) * ROUNDS_PER_WALL + max(0, required - have) * ROUNDS_PER_STONE
    # 最低墙线单独算工时：经济工只按"最低防线来不来得及"决定要不要帮忙，
    # 不能因为普通扩墙没修完就被长期拉去当石工。
    minimum_slots = slots[:max(0, minimum - built)]
    minimum_stone = stone_needed_for(len(minimum_slots))
    minimum_eta = (len(minimum_slots) * ROUNDS_PER_WALL
                   + max(0, minimum_stone - have) * ROUNDS_PER_STONE)
    program = {
        'assigned_builder': builder,
        'assigned_helper': helper,
        'target_wall_slots': [list(p) for p in target_slots],
        'wall_minimum_target': minimum,
        'wall_feasible_target': feasible,
        'walls_built': built,
        'walls_missing': len(slots),
        'required_stone': required,
        'stone_in_inventory_by_role': stone_by_role,
        'expected_finish_round': cycle + eta,
        'wall_deadline': deadline,
        'wall_slack': None if deadline is None else deadline - cycle - eta,
        'minimum_finish_eta': minimum_eta,
        'minimum_slack': None if deadline is None else deadline - cycle - minimum_eta,
        'stage': builder_state(state, builder),
        'takeover': takeover,
    }
    memory.update(assigned_builder=builder, assigned_helper=helper)
    if takeover:
        trace(state, builder, 'wall_program_takeover', '城墙工作单交接，不随角色阵亡消失',
              reason=takeover, previous_builder=previous_builder, new_builder=builder)
    return program


def builder_state(state, role_id, value=None):
    """读写建造工的持久状态；跨回合保留，避免每回合从零开始重新决策。"""
    memory = state.policy_memory.setdefault(BUILDER_STATE_KEY, {})
    key = str(role_id)
    if value is not None:
        memory[key] = value
        return value
    return memory.get(key)


# ---------------------------------------------------------------- 夜前剩余价值：候选计划

def plan_candidates(state, role, blocked, reserved=()):
    """首批关键墙完成后，比较 GATHER_ONLY / GATHER_AND_BUILD / BUILD_FROM_INVENTORY。

    模拟到 return_deadline 为止，排除无法安全回防的候选；当天建不完的石头留到第二天也算收益。
    """
    from .brain import own_station
    from .opening import adjacent_path
    rounds = rounds_before_return(state, role, blocked)
    stones = stone_count(role)
    free = free_slots(role)
    slots = remaining_wall_slots(state)
    mines = [z for z in (state.map_info.zones if state.map_info else [])
             if z.neutral_type == 'stone']
    mine_steps = None
    for mine in mines:
        path = adjacent_path(role, mine.pos, set(blocked) | set(reserved), state)
        if path is not None and (mine_steps is None or len(path) < mine_steps):
            mine_steps = len(path)
    base = own_station(state)
    wall_steps = None
    if slots and base is not None:
        from .protocol import Pos
        from .opening import wall_approach_path
        for point in slots[:4]:
            path = wall_approach_path(role, Pos(*point), set(blocked) | set(reserved), state)
            if path is not None and (wall_steps is None or len(path) < wall_steps):
                wall_steps = len(path)

    def row(name, extra_walls, carried, finish, idle, risk):
        return {
            'candidate': name, 'extra_walls_built': extra_walls,
            'stone_carried_to_day2': carried, 'expected_finish_round': finish,
            'can_return_safely': finish <= rounds, 'path_risk_penalty': risk,
            'idle_rounds': idle,
            'score': (extra_walls * WALL_VALUE + carried * STONE_VALUE
                      - risk * RISK_PENALTY - idle * IDLE_PENALTY),
        }

    candidates = []
    # A. GATHER_ONLY：去石矿一直采到背包满或必须回防；当天建不完也把石头带到第二天。
    if mine_steps is not None and (free is None or free > 0):
        budget = max(0, rounds - mine_steps)
        gained = budget if free is None else min(free, budget)
        candidates.append(row('GATHER_ONLY', 0, stones + gained, mine_steps + gained, 0, 0))
    # B. GATHER_AND_BUILD：采一批，回墙线连续建，再回防。
    if mine_steps is not None and wall_steps is not None and slots:
        overhead = mine_steps + wall_steps
        budget = max(0, rounds - overhead)
        pick = budget // (ROUNDS_PER_WALL + ROUNDS_PER_STONE)
        if free is not None:
            pick = min(pick, free)
        built = min(pick, len(slots))
        used = built * (ROUNDS_PER_WALL + ROUNDS_PER_STONE)
        candidates.append(row('GATHER_AND_BUILD', built, stones,
                              overhead + used, max(0, rounds - overhead - used), 0))
    # C. BUILD_FROM_INVENTORY：先把手上的石头连续建完，再看还能不能去采。
    if stones > 0 and wall_steps is not None and slots:
        built = min(stones, len(slots), max(0, rounds - wall_steps) // ROUNDS_PER_WALL)
        used = wall_steps + built * ROUNDS_PER_WALL
        carried = stones - built
        left = max(0, rounds - used)
        gained = 0
        if mine_steps is not None and left > mine_steps:
            budget = left - mine_steps
            gained = budget if free is None else min(free + built, budget)
            used += mine_steps + gained
        candidates.append(row('BUILD_FROM_INVENTORY', built, carried + gained, used, 0, 0))
    safe = [c for c in candidates if c['can_return_safely']]
    pool = safe or []
    selected = max(pool, key=lambda c: c['score']) if pool else None
    return candidates, selected


# ---------------------------------------------------------------- 汇总

def compute_work_orders(state, blocked, force=False):
    """每回合生成一次全局工作单并缓存。角色不再各自抢优先级。"""
    cached = getattr(state, WORK_ORDERS_KEY, None)
    if not force and isinstance(cached, dict) and cached.get('round') == state.round_no:
        return cached
    orders = {
        'round': state.round_no,
        'emergency_defense': emergency_defense(state),
        'weapon_minimum': weapon_minimum(state),
        'first_upgrade': first_upgrade(state, blocked),
        'wall_program': wall_program(state, blocked),
    }
    setattr(state, WORK_ORDERS_KEY, orders)
    return orders


def log_work_orders(state, orders):
    wall = orders['wall_program']
    upgrade = orders['first_upgrade']
    trace(state, None, 'work_orders', '本回合全局工作单',
          emergency_defense=orders['emergency_defense']['active'],
          emergency_reasons=orders['emergency_defense']['reasons'],
          weapon_minimum=orders['weapon_minimum'],
          wall_minimum_target=wall['wall_minimum_target'],
          wall_feasible_target=wall['wall_feasible_target'],
          walls_built=wall['walls_built'], walls_missing=wall['walls_missing'],
          wall_deadline=wall['wall_deadline'], wall_slack=wall['wall_slack'],
          wall_program_owner=wall['assigned_builder'], wall_program_helper=wall['assigned_helper'],
          required_stone=wall['required_stone'],
          stone_in_inventory_by_role=wall['stone_in_inventory_by_role'],
          expected_finish_round=wall['expected_finish_round'],
          takeover=wall['takeover'],
          first_upgrade_stage=upgrade['upgrade_stage'], upgrade_owner=upgrade['upgrade_owner'],
          upgrade_target_weapon=upgrade['upgrade_target_weapon'],
          funding_gap=upgrade['funding_gap'])
