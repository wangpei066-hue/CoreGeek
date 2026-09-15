"""第一天：三座火箭（两门最后一排、一门再靠前一格）-> 采铜铁卖钱升一门到2级 -> 采石修迎敌墙 -> 夜间三人三炮。

墙线是候选几何规划，不是官方合法区域；以快照中的建筑判断完成。
"""
from collections import deque
from itertools import permutations

from .protocol import Pos
from .grid import build_blocked_set, chebyshev, neighbors8
from .decision_log import trace, selected

WALL_MARGIN = 2
STONE_BATCH = 6  # 墙阶段两名工人各备半圈，减少往返。
MUSTER_BUFFER = 3
# 首日夜防到位是观测参数，不是官方昼夜分界（官方仍是白天70、夜晚60）。
# 约 70–80 机器人在走路；2 级火箭约 76 开火。操作 2 级火箭的人 75 前到位，其他人 76 前到位。
DAY1_L2_GUNNER_READY_CYCLE = 75
DAY1_OTHER_READY_CYCLE = 76
DAY1_WALL_TARGET = 8
DAY2_WALL_TARGET = 12
DAY1_WEAPON_L2_TARGET = 2  # 首日最好升两门到 2 级；钱或回合不够则只升一门。
WALL_STEP_SLACK = 1    # 每段墙在建造外再留1回合走位。
LATE_BUILD_SLACK = 2   # 墙工时 overrun 的初值，随后按入夜时是否仍缺墙调整。
MAX_WALL_OVERRUN = 12


def path_to_any(start, goals, blocked, width, height):
    """返回最短路径（不含起点）；[]为已到达，None为不可达。"""
    origin = (start.x, start.y)
    parents = {origin: None}
    queue = deque([origin])
    while queue:
        current = queue.popleft()
        if current in goals:
            path = []
            while parents[current] is not None:
                path.append(Pos(*current))
                current = parents[current]
            return path[::-1]
        for cell in neighbors8(Pos(*current), width, height):
            key = (cell.x, cell.y)
            if key not in blocked and key not in parents:
                parents[key] = current
                queue.append(key)
    return None


def adjacent_path(role, target, blocked, state):
    goals = {(p.x, p.y) for p in neighbors8(target, state.map_info.width, state.map_info.height)
             if (p.x, p.y) not in blocked or p == role.pos}
    return path_to_any(role.pos, goals, blocked, state.map_info.width, state.map_info.height)


def courtyard_cells(state, base):
    left, right, bottom, top = defense_bounds(state, base)
    return {(x, y) for x in range(left + 1, right) for y in range(bottom + 1, top)}


def in_courtyard(state, base, pos):
    return (pos.x, pos.y) in courtyard_cells(state, base)


def courtyard_anchor(state, base, blocked):
    cells = sorted(courtyard_cells(state, base),
                   key=lambda p: (max(abs(p[0] - base.pos.x), abs(p[1] - base.pos.y)), p))
    for cell in cells:
        if cell not in blocked:
            return Pos(*cell)
    return Pos(base.pos.x, base.pos.y)


def wall_approach_path(role, target, blocked, state, extra_avoid=()):
    """从基地一侧接近墙；禁止把迎敌外侧邻接格当落脚点，否则会贴外墙空转。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None:
        return adjacent_path(role, target, blocked, state)
    yard = courtyard_cells(state, base)
    direction = attack_direction(state, base)
    wall_dist = chebyshev(target, Pos(base.pos.x, base.pos.y))
    goals = set()
    for cell in neighbors8(target, state.map_info.width, state.map_info.height):
        key = (cell.x, cell.y)
        if key in blocked and cell != role.pos:
            continue
        if key in yard:
            goals.add(key)
            continue
        if (cell.x - target.x) * direction > 0:
            continue
        if chebyshev(cell, Pos(base.pos.x, base.pos.y)) <= wall_dist:
            goals.add(key)
    if not goals:
        return None
    return path_to_any(role.pos, goals, blocked | set(extra_avoid), state.map_info.width, state.map_info.height)


def interior_retreat_path(role, blocked, state):
    """墙外空转时先回到院内，而不是贴着外墙绕圈。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None:
        return None
    yard = courtyard_cells(state, base)
    if not yard:
        return None
    if (role.pos.x, role.pos.y) in yard:
        return []
    return path_to_any(role.pos, yard, blocked, state.map_info.width, state.map_info.height)


def defense_bounds(state, base):
    # 靠地图边缘时，地图边界充当屏障，墙线收缩到地图内。
    left = max(0, base.pos.x - WALL_MARGIN)
    right = min(state.map_info.width - 1, base.pos.x + 1 + WALL_MARGIN)
    bottom = max(0, base.pos.y - 1 - WALL_MARGIN)
    top = min(state.map_info.height - 1, base.pos.y + WALL_MARGIN)
    return left, right, bottom, top


def attack_direction(state, base):
    """用户确认：左上基地受右侧进攻，右下基地受左侧进攻；按实际坐标换边。"""
    return 1 if base.pos.x + 0.5 < (state.map_info.width - 1) / 2 else -1


def wall_priority(state, base, point):
    left, right, _, _ = defense_bounds(state, base)
    front = right if attack_direction(state, base) == 1 else left
    if point[0] == front:
        return 0  # 先形成完整内层，避免外层开口直通基地。
    return 1 if point[0] == front + 2*attack_direction(state, base) else 2


def funnel_gap(state, base):
    """外层中部单格开口；与内层之间留一格，后方作为己方主通道。"""
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    outer_x = (right if direction == 1 else left) + 2*direction
    if not 0 <= outer_x < state.map_info.width:
        return None  # 地图边缘放不下第二层，退化为单层并记录。
    return outer_x, (bottom + top)//2


def movement_avoid(state):
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    gap = funnel_gap(state, base) if base else None
    # 己方寻路不把诱敌开口当通道；后方没有新建墙。
    return {gap} if gap else set()


def rear_weapon_x(state, base):
    """最后一排：院子里远离进攻方向的那一列，给修墙留出前线通道。"""
    left, right, _, _ = defense_bounds(state, base)
    return left + 1 if attack_direction(state, base) == 1 else right - 1


def weapon_slots(state, base):
    """两门放最后一排上下两侧，一门放到另一侧再靠前一格，避免堵在迎敌墙内侧。"""
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    rear_x = rear_weapon_x(state, base)
    forward_x = rear_x + direction
    y_low, y_high = bottom + 1, top - 1
    width, height = state.map_info.width, state.map_info.height
    slots = [(rear_x, y_low), (rear_x, y_high), (forward_x, y_high)]
    station = {(base.pos.x + dx, base.pos.y - dy) for dx in (0, 1) for dy in (0, 1)}
    cleaned = []
    for x, y in slots:
        if not (0 <= x < width and 0 <= y < height) or (x, y) in station:
            continue
        cleaned.append((x, y))
    return cleaned


def wall_ring(state, base):
    """迎敌双层防线：内层完整、外层留口、侧翼补墙，后方开放。"""
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    front = right if direction == 1 else left
    protected_rear = rear_weapon_x(state, base)
    cells = {(front, y) for y in range(bottom, top + 1)}
    gap = funnel_gap(state, base)
    if gap:
        cells.update((gap[0], y) for y in range(bottom, top + 1) if (gap[0], y) != gap)
    cells.update((x, y) for x in range(left, right + 1) for y in (bottom, top)
                 if (x - protected_rear) * direction >= 0)
    return sorted(cells, key=lambda p: (wall_priority(state, base, p), p))


def primary_wall_plan(state, base):
    gap = funnel_gap(state, base)
    return [p for p in wall_ring(state, base) if gap is None or p[0] != gap[0]]


def day_index(state):
    return (state.round_no or 0) // 130


def _shortest_adjacent(roles, targets, blocked, state):
    best = None
    for role in roles:
        for target in targets:
            path = adjacent_path(role, target, blocked, state)
            if path is None:
                continue
            cost = len(path)
            if best is None or cost < best:
                best = cost
    return best


def wall_finish_rounds(state, missing, blocked):
    """两名工人补完当前阶段墙的回合下界：缺石采集 + 走到缺口 + 每段建造。"""
    n = len(missing)
    if n <= 0:
        return 0
    workers = [r for r in state.team_our.roles if r.role_type == 'worker' and r.health > 0]
    hands = max(1, len(workers))
    stones = sum(r.backpack.count('stone') for r in workers)
    collect = max(0, n - stones)
    mines = [z.pos for z in state.map_info.zones if z.neutral_type == 'stone']
    mine_travel = 0
    if collect and mines:
        mine_travel = _shortest_adjacent(workers, mines, blocked, state)
        if mine_travel is None:
            return 10 ** 6
    gaps = [Pos(*p) for p in missing]
    gap_travel = 0
    if gaps:
        gap_travel = _shortest_adjacent(workers, gaps, blocked, state)
        if gap_travel is None:
            return 10 ** 6
    return mine_travel + -(-collect // hands) + gap_travel + n * WALL_STEP_SLACK + -(-n // hands)


def day_rounds_remaining(round_no):
    """当前白天周期内距离官方入夜（cycle 70）的剩余回合；夜间为 0。"""
    from .brain import DAY_NIGHT_CYCLE, DAY_ROUNDS
    if round_no is None:
        return DAY_ROUNDS
    cycle = int(round_no) % DAY_NIGHT_CYCLE
    if cycle >= DAY_ROUNDS:
        return 0
    return DAY_ROUNDS - cycle


def defense_ready_cycle(state, role=None):
    """本周期应回到炮位的 cycle。次日仍按官方 70；首日按 2 级火箭开火观测放宽。"""
    from .brain import DAY_ROUNDS, WEAPON_TYPES
    if (state.round_no or 0) // 130 > 0:
        return DAY_ROUNDS
    if role is not None:
        weapon = assign_weapons(state).get(role.id)
        if weapon and weapon.role_type == 'rocket' and (weapon.level or 1) >= 2:
            return DAY1_L2_GUNNER_READY_CYCLE
        return DAY1_OTHER_READY_CYCLE
    rockets = [r for r in (state.team_our.roles if state.team_our else [])
               if r.role_type == 'rocket' and r.health > 0 and (r.level or 1) >= 2]
    return DAY1_L2_GUNNER_READY_CYCLE if rockets else DAY1_OTHER_READY_CYCLE


def defense_rounds_remaining(state, role=None):
    """距该角色夜防到位点的剩余回合；过点为 0。"""
    from .brain import DAY_NIGHT_CYCLE
    cycle = (state.round_no or 0) % DAY_NIGHT_CYCLE
    return max(0, defense_ready_cycle(state, role) - cycle)


def first_night_economy_open(state):
    """首日官方入夜后、2 级火箭开火前：无可见敌人时仍走开局经济。"""
    n = state.round_no or 0
    if n >= 130 or n < 70:
        return False
    from .tactics import imminent_contact, pressure, threat_robots
    if pressure(state) or imminent_contact(state) or threat_robots(state):
        return False
    return (n % 130) < DAY1_OTHER_READY_CYCLE


def _actor_at(role, pos):
    from dataclasses import replace
    return replace(role, pos=pos)


def _walk_adjacent(role, target, blocked, state):
    """走到目标邻格。返回 (步数, 站立后的角色)；不可达返回 None。"""
    path = adjacent_path(role, target, blocked, state)
    if path is None:
        return None
    stand = role.pos if not path else path[-1]
    return len(path), _actor_at(role, stand)


def _blank_upgrade_estimate(**overrides):
    est = {
        'ok': False,
        'status': 'unreachable',
        'funding_deficit': 0,
        'inventory_sale_value': 0,
        'mine_rounds': 0,
        'route_rounds': 0,
        'action_rounds': 0,
        'total': None,
        'fallback_reason': None,
        'actor_id': None,
        'ore': None,
    }
    est.update(overrides)
    return est


def _finish_upgrade_estimate(role, actor, blocked, state, status, deficit, inventory_value,
                             mine_rounds, route_rounds, action_rounds, ore=None):
    gun_back = station_return_steps(actor, state, blocked)
    if gun_back is None:
        return _blank_upgrade_estimate(
            status=status, funding_deficit=deficit, inventory_sale_value=inventory_value,
            mine_rounds=mine_rounds, route_rounds=route_rounds, action_rounds=action_rounds,
            fallback_reason='night_post_unreachable', actor_id=role.id, ore=ore,
        )
    route_rounds += gun_back
    total = route_rounds + action_rounds + mine_rounds
    return _blank_upgrade_estimate(
        ok=True, status=status, funding_deficit=deficit, inventory_sale_value=inventory_value,
        mine_rounds=mine_rounds, route_rounds=route_rounds, action_rounds=action_rounds,
        total=total, actor_id=role.id, ore=ore,
    )


def _estimate_actor_upgrade(role, state, blocked, gold, voucher_cost, weapon, prices):
    """同一角色的连续升级链路：持券 / 买券 / 卖矿买券 / 采矿卖矿买券。"""
    from .economy import metal_inventory_value, voucher_collect_plan
    if role.health <= 0 or weapon is None:
        return _blank_upgrade_estimate(fallback_reason='no_actor_or_weapon', actor_id=getattr(role, 'id', None))
    inventory_value = metal_inventory_value(role, state)
    metals = [name for name in ('iron', 'copper') if name in role.backpack]
    has_voucher = any(item == 'WeaponUpgradeVoucher1' for item in role.backpack)
    actor = role
    route_rounds = 0
    action_rounds = 0
    mine_rounds = 0
    ore = None
    deficit = max(0, voucher_cost - gold)

    if has_voucher:
        walked = _walk_adjacent(actor, weapon.pos, blocked, state)
        if walked is None:
            return _blank_upgrade_estimate(
                status='have_voucher', inventory_sale_value=inventory_value,
                fallback_reason='upgrade_target_unreachable', actor_id=role.id,
            )
        steps, actor = walked
        route_rounds += steps
        action_rounds += 1
        return _finish_upgrade_estimate(
            role, actor, blocked, state, 'have_voucher', 0, inventory_value,
            mine_rounds, route_rounds, action_rounds,
        )

    if gold < voucher_cost:
        if role.role_type != 'worker':
            return _blank_upgrade_estimate(
                status='need_mine', funding_deficit=deficit, inventory_sale_value=inventory_value,
                fallback_reason='pioneer_cannot_sell_or_mine', actor_id=role.id,
            )
        remaining_value = max(0, deficit - inventory_value)
        need_mine = remaining_value > 0
        if need_mine:
            plans = []
            for mine in state.map_info.zones if state.map_info else []:
                if mine.neutral_type not in ('iron', 'copper'):
                    continue
                plan = voucher_collect_plan(
                    actor, state, blocked, set(), mine, remaining_value, prices=prices,
                )
                if plan is None:
                    continue
                plans.append((mine, plan))
            fitting = [(mine, plan) for mine, plan in plans if plan['fits_backpack']]
            if not fitting:
                if not any(prices.get(n, 0) > 0 for n in ('iron', 'copper')):
                    reason = 'metal_price_unknown'
                elif not plans:
                    reason = 'mine_unreachable'
                else:
                    reason = 'backpack_capacity'
                mine_rounds = min((plan['units'] for _mine, plan in plans), default=0)
                ore = None if not plans else min(plans, key=lambda item: item[1]['units'])[0].neutral_type
                return _blank_upgrade_estimate(
                    status='need_mine', funding_deficit=deficit, inventory_sale_value=inventory_value,
                    mine_rounds=mine_rounds, fallback_reason=reason, actor_id=role.id, ore=ore,
                )
            mine, plan = min(fitting, key=lambda item: (item[1]['rounds'], item[1]['units'], item[1]['path_len']))
            walked = _walk_adjacent(actor, mine.pos, blocked, state)
            if walked is None:
                return _blank_upgrade_estimate(
                    status='need_mine', funding_deficit=deficit, inventory_sale_value=inventory_value,
                    fallback_reason='mine_unreachable', actor_id=role.id,
                )
            steps, actor = walked
            route_rounds += steps
            mine_rounds = plan['units']
            ore = mine.neutral_type
            metals = sorted(set(metals + [ore]))
        from .brain import find_zone
        vendor = find_zone(state, 'vendor')
        if vendor is None:
            return _blank_upgrade_estimate(
                status='need_mine' if mine_rounds else 'sell_inventory',
                funding_deficit=deficit, inventory_sale_value=inventory_value,
                mine_rounds=mine_rounds, route_rounds=route_rounds,
                fallback_reason='vendor_unreachable', actor_id=role.id, ore=ore,
            )
        walked = _walk_adjacent(actor, vendor.pos, blocked, state)
        if walked is None:
            return _blank_upgrade_estimate(
                status='need_mine' if mine_rounds else 'sell_inventory',
                funding_deficit=deficit, inventory_sale_value=inventory_value,
                mine_rounds=mine_rounds, route_rounds=route_rounds,
                fallback_reason='vendor_unreachable', actor_id=role.id, ore=ore,
            )
        steps, actor = walked
        route_rounds += steps
        action_rounds += max(1, len(metals))
        status = 'need_mine' if mine_rounds else 'sell_inventory'
    else:
        status = 'gold_ready'
        deficit = 0

    from .brain import find_zone
    shop = find_zone(state, 'weaponShop')
    if shop is None:
        return _blank_upgrade_estimate(
            status=status, funding_deficit=deficit, inventory_sale_value=inventory_value,
            mine_rounds=mine_rounds, route_rounds=route_rounds, action_rounds=action_rounds,
            fallback_reason='shop_unreachable', actor_id=role.id, ore=ore,
        )
    walked = _walk_adjacent(actor, shop.pos, blocked, state)
    if walked is None:
        return _blank_upgrade_estimate(
            status=status, funding_deficit=deficit, inventory_sale_value=inventory_value,
            mine_rounds=mine_rounds, route_rounds=route_rounds, action_rounds=action_rounds,
            fallback_reason='shop_unreachable', actor_id=role.id, ore=ore,
        )
    steps, actor = walked
    route_rounds += steps
    action_rounds += 1
    walked = _walk_adjacent(actor, weapon.pos, blocked, state)
    if walked is None:
        return _blank_upgrade_estimate(
            status=status, funding_deficit=deficit, inventory_sale_value=inventory_value,
            mine_rounds=mine_rounds, route_rounds=route_rounds, action_rounds=action_rounds,
            fallback_reason='upgrade_target_unreachable', actor_id=role.id, ore=ore,
        )
    steps, actor = walked
    route_rounds += steps
    action_rounds += 1
    return _finish_upgrade_estimate(
        role, actor, blocked, state, status, deficit, inventory_value,
        mine_rounds, route_rounds, action_rounds, ore=ore,
    )


def estimate_opening_upgrade(state, blocked, gold=None, pending_targets=None):
    """按真实连续路线估算升一门到 2 级所需回合；不可达返回 ok=False、total=None。"""
    from .brain import WEAPON_TYPES, _pick_upgradeable, item_cost
    from .economy import metal_inventory_value, ore_prices
    gold = state.team_our.gold_num if gold is None and state.team_our else (0 if gold is None else gold)
    voucher_cost = item_cost('WeaponUpgradeVoucher1', state)
    prices = ore_prices(state)
    pending = set() if pending_targets is None else set(pending_targets)
    weapon = _pick_upgradeable(state, WEAPON_TYPES, pending, max_current_level=1)
    if weapon is None:
        weapon = _pick_upgradeable(state, WEAPON_TYPES, pending, max_current_level=2)
    fighters = [r for r in (state.team_our.roles if state.team_our else [])
                if r.role_type in ('worker', 'pioneer') and r.health > 0]
    inventory_total = sum(metal_inventory_value(r, state) for r in fighters if r.role_type == 'worker')
    holders = [r for r in fighters if 'WeaponUpgradeVoucher1' in r.backpack]
    if holders:
        candidates = holders
    elif gold >= voucher_cost:
        candidates = fighters
    else:
        candidates = [r for r in fighters if r.role_type == 'worker']
    if weapon is None:
        return _blank_upgrade_estimate(
            funding_deficit=max(0, voucher_cost - gold),
            inventory_sale_value=inventory_total,
            fallback_reason='no_upgrade_target',
        )
    if not candidates:
        return _blank_upgrade_estimate(
            funding_deficit=max(0, voucher_cost - gold),
            inventory_sale_value=inventory_total,
            fallback_reason='no_actor',
        )
    best = None
    worst_fail = None
    for role in candidates:
        est = _estimate_actor_upgrade(role, state, blocked, gold, voucher_cost, weapon, prices)
        if est.get('ok'):
            if best is None or (est['total'], role.id) < (best['total'], best.get('actor_id') or 0):
                best = est
        elif worst_fail is None or (est.get('fallback_reason') or '') > (worst_fail.get('fallback_reason') or ''):
            worst_fail = est
    if best is not None:
        return best
    fail = worst_fail or _blank_upgrade_estimate(
        funding_deficit=max(0, voucher_cost - gold),
        inventory_sale_value=inventory_total,
        fallback_reason='unreachable',
    )
    fail['inventory_sale_value'] = fail.get('inventory_sale_value') or inventory_total
    fail['funding_deficit'] = max(fail.get('funding_deficit') or 0, max(0, voucher_cost - gold))
    return fail


def voucher_trip_rounds(state, blocked, gold, need_sell=True):
    """兼容旧调用：返回完整升级链路总回合，不可达为 None。"""
    est = estimate_opening_upgrade(state, blocked, gold)
    return est['total'] if est.get('ok') else None


def reserved_unbought_weapon_gold(state):
    from .brain import item_cost
    total = 0
    for role in (state.team_our.roles if state.team_our else []):
        job = state.worker_item_jobs.get(role.id)
        if job and job.get('kind') == 'weapon' and job.get('item') not in role.backpack:
            total += item_cost(job['item'], state)
    return total


def live_l2_weapon_count(state):
    from .brain import WEAPON_TYPES
    return sum(1 for r in (state.team_our.roles if state.team_our else [])
               if r.role_type in WEAPON_TYPES and r.health > 0 and (r.level or 1) >= 2)


def day1_second_upgrade_fits(state, blocked=None):
    """首日第二门：现金+背包矿够一张券，且升级链路能在到位点前完成。"""
    from .brain import _pending_item_job_targets, item_cost
    from .economy import metal_inventory_value
    from .grid import build_blocked_set
    if (state.round_no or 0) // 130 > 0:
        return False
    if blocked is None:
        blocked = build_blocked_set(state)
    cost = item_cost('WeaponUpgradeVoucher1', state)
    gold = (state.team_our.gold_num if state.team_our else 0) - reserved_unbought_weapon_gold(state)
    inventory = sum(metal_inventory_value(r, state)
                    for r in (state.team_our.roles if state.team_our else [])
                    if r.role_type == 'worker' and r.health > 0)
    if gold + inventory < cost:
        return False
    remaining = defense_rounds_remaining(state)
    est = estimate_opening_upgrade(
        state, blocked, gold=max(0, gold), pending_targets=_pending_item_job_targets(state),
    )
    total = est.get('total')
    if not est.get('ok') or total is None:
        return False
    return remaining > total + MUSTER_BUFFER


def opening_time_budget(state, missing, remaining, muster_need, gold, upgraded_once, blocked):
    """首日切换点：完整升级链路来得及则采铜铁升炮；券/金币就绪则升炮与修墙并行。"""
    wall_need = wall_finish_rounds(state, missing, blocked)
    wall_deadline = wall_need + muster_need
    can_finish_walls = remaining > wall_deadline
    upgraded_count = int(upgraded_once) if isinstance(upgraded_once, bool) else int(upgraded_once or 0)
    pending = None
    if upgraded_count >= 1:
        from .brain import _pending_item_job_targets
        pending = _pending_item_job_targets(state)
    est = estimate_opening_upgrade(state, blocked, gold, pending_targets=pending)
    if upgraded_count >= DAY1_WEAPON_L2_TARGET:
        est = dict(est)
        est['status'] = 'already_upgraded'
        est['ok'] = True
        est['fallback_reason'] = None
    budget = {
        'wall_need': wall_need, 'wall_deadline': wall_deadline, 'sell_trip': est.get('total'),
        'can_finish_walls': can_finish_walls,
        'upgrade_status': est.get('status'),
        'funding_deficit': est.get('funding_deficit') or 0,
        'inventory_sale_value': est.get('inventory_sale_value') or 0,
        'mine_rounds': est.get('mine_rounds') or 0,
        'route_rounds': est.get('route_rounds') or 0,
        'action_rounds': est.get('action_rounds') or 0,
        'total_upgrade_rounds': est.get('total'),
        'fallback_reason': est.get('fallback_reason'),
        'upgraded_count': upgraded_count,
    }
    if upgraded_count >= DAY1_WEAPON_L2_TARGET:
        budget.update(allow_walls=True, allow_upgrade=False, allow_sell=False, allow_mine=False)
        return budget
    has_voucher = any(
        'WeaponUpgradeVoucher1' in r.backpack
        for r in (state.team_our.roles if state.team_our else [])
        if r.role_type in ('worker', 'pioneer') and r.health > 0
    )
    from .brain import item_cost
    if has_voucher or gold >= item_cost('WeaponUpgradeVoucher1', state):
        budget.update(allow_walls=True, allow_upgrade=True, allow_sell=False, allow_mine=False)
        budget['upgrade_status'] = 'have_voucher' if has_voucher else 'gold_ready'
        budget['fallback_reason'] = None
        return budget
    total = est.get('total')
    if not est.get('ok') or total is None or remaining <= total:
        budget.update(allow_walls=True, allow_upgrade=False, allow_sell=False, allow_mine=False)
        if not budget.get('fallback_reason'):
            budget['fallback_reason'] = 'upgrade_too_late' if est.get('ok') else (est.get('fallback_reason') or 'unreachable')
        return budget
    budget.update(allow_walls=False, allow_upgrade=True, allow_sell=True, allow_mine=True)
    return budget



def staged_wall_plan(state, base):
    """按天限制迎敌墙数量：首日正面约8段，次日补到12段，之后再铺满一层。"""
    plan = primary_wall_plan(state, base)
    day = day_index(state)
    if day <= 0:
        return plan[:DAY1_WALL_TARGET]
    if day == 1:
        return plan[:DAY2_WALL_TARGET]
    return plan


def staged_wall_missing(state):
    from .brain import own_station
    base = own_station(state)
    if base is None:
        return []
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    return [p for p in staged_wall_plan(state, base) if p not in existing]


def station_return_detail(role, state, blocked, from_pos=None):
    """回炮步数及原因。已在操炮/基地邻格可以为 0；无路径、无炮位、无基地是未知，不能记成 0。"""
    from dataclasses import replace
    actor = replace(role, pos=from_pos) if from_pos is not None else role
    stand = {'x': actor.pos.x, 'y': actor.pos.y}
    weapon = assign_weapons(state).get(role.id)
    if weapon is not None and chebyshev(actor.pos, weapon.pos) <= 1 and actor.pos != weapon.pos:
        return dict(steps=0, reason='already_at_weapon', alreadyAtPost=True,
                    weaponId=weapon.id, stand=stand)
    if weapon is None:
        base = next((r for r in state.team_our.roles if r.role_type == 'station' and r.health > 0), None)
        if base is None:
            return dict(steps=None, reason='no_station_or_weapon', alreadyAtPost=False,
                        weaponId=None, stand=stand)
        if chebyshev(actor.pos, base.pos) <= 1 and actor.pos != base.pos:
            return dict(steps=0, reason='already_at_station', alreadyAtPost=True,
                        weaponId=None, stand=stand)
        path = adjacent_path(actor, base.pos, blocked, state)
        if path is None:
            return dict(steps=None, reason='no_path_to_station', alreadyAtPost=False,
                        weaponId=None, stand=stand)
        return dict(steps=len(path), reason='already_at_station' if not path else 'path_to_station',
                    alreadyAtPost=not path, weaponId=None, stand=stand)
    path = station_path(actor, weapon, blocked, state)
    if path is None:
        return dict(steps=None, reason='no_path_to_weapon', alreadyAtPost=False,
                    weaponId=weapon.id, stand=stand)
    return dict(steps=len(path), reason='already_at_weapon' if not path else 'path_to_weapon',
                alreadyAtPost=not path, weaponId=weapon.id, stand=stand)


def station_return_steps(role, state, blocked, from_pos=None):
    """走到操炮位（或基地旁）的步数；找不到路返回 None，不能当成固定 8 回合可达。"""
    return station_return_detail(role, state, blocked, from_pos=from_pos)['steps']


def wall_overrun_margin(state):
    return int((state.policy_memory or {}).get('wall_time_overrun', LATE_BUILD_SLACK))


def update_wall_time_overrun(state):
    """每个夜晚起点比较「计划是否完工」，加大或回收施工余量。不是官方耗时。"""
    if (state.round_no or 0) % 130 != 70:
        return
    overrun = wall_overrun_margin(state)
    front_missing = critical_wall_missing(state)
    stage_missing = staged_wall_missing(state)
    attempted = bool((state.policy_memory or {}).pop('wall_work_attempted', False))
    still_open = bool(front_missing or stage_missing)
    if still_open and attempted:
        state.policy_memory['wall_time_overrun'] = min(MAX_WALL_OVERRUN, overrun + 2)
        trace(state, None, 'wall_time_overrun', '已安排施工但入夜仍缺墙，加大工时余量',
              overrun=state.policy_memory['wall_time_overrun'],
              missing=len(set(front_missing) | set(stage_missing)))
    elif still_open:
        trace(state, None, 'wall_unfinished_other', '入夜仍缺墙，但本昼未见施工，不把缺石/采购/非法当成工时低估',
              missing=len(set(front_missing) | set(stage_missing)))
    elif overrun > LATE_BUILD_SLACK:
        state.policy_memory['wall_time_overrun'] = overrun - 1


def critical_wall_missing(state):
    """正面迎敌列缺口：直接挡基地和炮位，8/12 段上限不能代表这条线已经有效。"""
    from .brain import own_station
    base = own_station(state)
    if base is None:
        return []
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    return [p for p in primary_wall_plan(state, base) if wall_priority(state, base, p) == 0 and p not in existing]


def worker_wall_muster_rounds(state, role, missing):
    """该工人补完分摊缺口并回到炮位的估计：到施工区 + 取石施工 + 回炮 + 历史余量。"""
    blocked = build_blocked_set(state) | movement_avoid(state)
    workers = [r for r in state.team_our.roles if r.role_type == 'worker' and r.health > 0]
    hands = max(1, len(workers))
    share = -(-len(missing) // hands)
    stones = role.backpack.count('stone') if role is not None else 0
    collect = max(0, share - stones)
    mines = [z.pos for z in state.map_info.zones if z.neutral_type == 'stone']
    mine_roles = [role] if role is not None else workers
    mine_travel = 0
    if collect and mines:
        mine_travel = _shortest_adjacent(mine_roles, mines, blocked, state)
        if mine_travel is None:
            return 10 ** 6
    gaps = [Pos(*p) for p in missing]
    if role is not None and gaps:
        lengths = []
        for gap in gaps:
            path = wall_approach_path(role, gap, blocked, state)
            if path is not None:
                lengths.append(len(path))
        gap_travel = min(lengths) if lengths else None
        gun_travel = station_return_steps(role, state, blocked)
        if gap_travel is None or gun_travel is None:
            return 10 ** 6
    else:
        gap_travel = _shortest_adjacent(workers, gaps, blocked, state) if gaps else 0
        if gaps and gap_travel is None:
            return 10 ** 6
        gun_travel = MUSTER_BUFFER
    return mine_travel + collect + gap_travel + share * WALL_STEP_SLACK + gun_travel + MUSTER_BUFFER + wall_overrun_margin(state)


def full_wall_build_window(state, role=None):
    """侧翼/外层：按该工人回炮时间开工，不用固定「剩余≤估计+5」。"""
    from .tactics import night_wave_cleared
    if night_wave_cleared(state):
        return True
    if first_night_economy_open(state) or (state.round_no or 0) < 70:
        return True
    if defense_rounds_remaining(state, role) <= 0:
        return False
    missing = staged_wall_missing(state)
    if not missing:
        return False
    return defense_rounds_remaining(state, role) <= worker_wall_muster_rounds(state, role, missing)


def worker_should_build_walls(state, role=None):
    """正面缺口有石就补；侧翼和外层等基本防线形成后再按回炮工时安排。"""
    from .brain import structure_priority_day
    from .tactics import night_wave_cleared, night_near_work_allowed
    if night_wave_cleared(state):
        return True
    if first_night_economy_open(state) or (state.round_no or 0) < 70:
        return True
    if defense_rounds_remaining(state, role) <= 0:
        return bool(night_near_work_allowed(state) and critical_wall_missing(state))
    if critical_wall_missing(state):
        return True
    if structure_priority_day(state):
        missing = staged_wall_missing(state)
        if not missing:
            return False
        need = worker_wall_muster_rounds(state, role, missing)
        return defense_rounds_remaining(state, role) > need
    return full_wall_build_window(state, role)


def stones_cover_wall_plan(state):
    missing = staged_wall_missing(state)
    if not missing:
        return False
    have = sum(r.backpack.count('stone') for r in state.team_our.roles
               if r.role_type == 'worker' and r.health > 0)
    return have >= len(missing)


def staged_walls_incomplete(state):
    return bool(staged_wall_missing(state))


def outer_wall_ready(state):
    """二层准入：一层完整、至少二级且血量80%；三座武器至少二级。"""
    from .brain import own_station, max_health
    base = own_station(state)
    if base is None:
        return False
    walls = {(r.pos.x, r.pos.y): r for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    weapons = [r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket') and r.health > 0]
    return (len(weapons) >= 3 and all((r.level or 1) >= 2 for r in weapons)
            and all(p in walls and (walls[p].level or 1) >= 2 and walls[p].health >= max_health(walls[p])*0.8
                    for p in primary_wall_plan(state, base)))


def active_wall_plan(state, base):
    return wall_ring(state, base) if outer_wall_ready(state) else primary_wall_plan(state, base)


def mobile_walkable(state, blocked, reserved=()):
    """寻路时忽略己方可移动角色的临时占位，避免队友互相卡住导致空转或缺炮。"""
    mobiles = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')}
    return (set(blocked) - mobiles) | set(reserved)


def _outerness(state, pos):
    """迎敌方向越靠前、离基地越远，越算外炮/外位。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None:
        return (pos.x, pos.y)
    direction = attack_direction(state, base)
    return ((pos.x - base.pos.x) * direction, chebyshev(pos, Pos(base.pos.x, base.pos.y)))


def _fighter_layer(state, role):
    """0=院内里侧，越大越靠外；用于里炮给人、外炮给外面的人。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    outside = 0 if base and in_courtyard(state, base, role.pos) else 1
    return (outside, _outerness(state, role.pos), role.id)


def _weapon_layer(state, weapon):
    return (_outerness(state, weapon.pos), weapon.id)


def _assignment_score(pairs, distances, fighter_order, weapon_order):
    unreachable = sum(distances[f.id, w.id] >= 10000 for f, w in pairs)
    parked = -sum(distances[f.id, w.id] == 0 for f, w in pairs)
    mismatch = sum(abs(fighter_order[f.id] - weapon_order[w.id]) for f, w in pairs)
    travel = sum(distances[f.id, w.id] for f, w in pairs)
    return (unreachable, parked, mismatch, travel)


def assign_weapons(state, excluded_ids=(), persist=False):
    """一人一炮：把队友当障碍，里侧的人开里炮、外侧的人开外炮，避免卡在通道里空转。"""
    fighters = sorted((r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0 and r.id not in excluded_ids), key=lambda r: r.id)
    weapons = sorted((r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket') and r.health > 0), key=lambda r: r.id)
    if not fighters or not weapons:
        return {}
    static = build_blocked_set(state) | movement_avoid(state)
    distances = {}
    for fighter in fighters:
        others = {(r.pos.x, r.pos.y) for r in fighters if r.id != fighter.id}
        blocked = (static | others) - {(fighter.pos.x, fighter.pos.y)}
        for weapon in weapons:
            path = adjacent_path(fighter, weapon.pos, blocked, state)
            distances[fighter.id, weapon.id] = len(path) if path is not None else 10000
    fighter_order = {f.id: i for i, f in enumerate(sorted(fighters, key=lambda r: _fighter_layer(state, r)))}
    weapon_order = {w.id: i for i, w in enumerate(sorted(weapons, key=lambda r: _weapon_layer(state, r)))}
    best = None
    assignment = {}
    count = min(len(fighters), len(weapons))
    for chosen in permutations(fighters, count):
        for targets in permutations(weapons, count):
            pairs = list(zip(chosen, targets))
            score = _assignment_score(pairs, distances, fighter_order, weapon_order)
            if best is None or score < best:
                best = score
                assignment = {f.id: w for f, w in pairs}
    prev = {}
    weapons_by_id = {w.id: w for w in weapons}
    for fid, wid in (state.policy_memory.get('weapon_assignment') or {}).items():
        try:
            fid, wid = int(fid), int(wid)
        except (TypeError, ValueError):
            continue
        fighter = next((r for r in fighters if r.id == fid), None)
        weapon = weapons_by_id.get(wid)
        if fighter and weapon:
            prev[fid] = weapon
    if len(prev) == count and len({w.id for w in prev.values()}) == count:
        prev_pairs = [(next(f for f in fighters if f.id == fid), weapon) for fid, weapon in prev.items()]
        prev_score = _assignment_score(prev_pairs, distances, fighter_order, weapon_order)
        if best is None or prev_score <= best:
            assignment = prev
    if persist:
        state.policy_memory['weapon_assignment'] = {str(fid): weapon.id for fid, weapon in assignment.items()}
    return assignment


def move_on_path(state, role, path, reserved, reason):
    if path:
        step = path[0]
        reserved.add((step.x, step.y))
        return selected(state, role.id, {'action': 'move', 'targetPos': [{'x': step.x, 'y': step.y}]}, reason)
    trace(state, role.id, 'at_destination' if path == [] else 'unreachable',
          '已到达目标位置' if path == [] else '当前目标不可达', task=reason)
    return None


def best_voucher_worker(state, workers, blocked):
    """把买券/用券交给已经持券或离商店最近的工人，避免远工占住任务。"""
    from .brain import find_zone
    holders = [w for w in workers
               if any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in w.backpack)]
    if holders:
        return min(holders, key=lambda w: w.id)
    shop = find_zone(state, 'weaponShop')
    if shop is None:
        return workers[0] if workers else None
    walkable = mobile_walkable(state, blocked)
    ranked = []
    for worker in workers:
        path = adjacent_path(worker, shop.pos, walkable, state)
        if path is not None:
            ranked.append((len(path), worker.id, worker))
    if ranked:
        return min(ranked)[2]
    return min(workers, key=lambda w: (chebyshev(w.pos, shop.pos), w.id)) if workers else None


def station_path(role, weapon, blocked, state):
    """操控位置不能停在未来墙体缺口上，避免堵塞回城通道。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    ring = set(wall_ring(state, base)) if base else set()
    goals = {(p.x, p.y) for p in neighbors8(weapon.pos, state.map_info.width, state.map_info.height)
             if (p.x, p.y) not in ring and ((p.x, p.y) not in blocked or p == role.pos)}
    return path_to_any(role.pos, goals, blocked, state.map_info.width, state.map_info.height)


def weapon_approach_path(role, weapon, blocked, reserved, state):
    """去开炮：先绕开队友，走不通再让路穿过占位，避免空转。"""
    own = {(role.pos.x, role.pos.y)}
    strict = adjacent_path(role, weapon.pos, (blocked | reserved) - own, state)
    if strict is not None:
        return strict
    return adjacent_path(role, weapon.pos, mobile_walkable(state, blocked, reserved), state)


def weapon_candidates(state, base, name, extra_names=(), extra_positions=()):
    """按编制空位补齐：先最后一排两门，再另一侧靠前一格。extra_names 仅兼容调用方。"""
    occupied = {(r.pos.x, r.pos.y) for r in state.team_our.roles
                if r.health > 0 and r.role_type in ('gatling', 'railgun', 'rocket', 'wall', 'station')}
    occupied.update((p[0], p[1]) for p in extra_positions)
    occupied.update((base.pos.x + dx, base.pos.y - dy) for dx in (0, 1) for dy in (0, 1))
    slots = [p for p in weapon_slots(state, base) if p not in occupied]
    if slots:
        return slots
    left, right, bottom, top = defense_bounds(state, base)
    fallback = [(x, y) for x in range(left + 1, right) for y in range(bottom + 1, top)
                if (x, y) not in occupied]
    return fallback


def pioneer_stay_clear(role, state, blocked, reserved, assignments=None):
    """开拓者不能采集或建造；让开墙线和炮位，无任务时去已分配武器。"""
    from .brain import own_station
    if role.role_type != 'pioneer' or role.health <= 0:
        return None
    base = own_station(state)
    if base is None:
        return None
    here = (role.pos.x, role.pos.y)
    construction = set(wall_ring(state, base)) | set(weapon_slots(state, base))
    own = {here}
    if here in construction:
        yard = courtyard_cells(state, base) - construction - ((blocked | reserved) - own)
        path = path_to_any(role.pos, yard, (blocked | reserved) - own,
                           state.map_info.width, state.map_info.height) if yard else None
        if path is None:
            path = interior_retreat_path(role, (blocked | reserved) - own, state)
        cmd = move_on_path(state, role, path, reserved, '开拓者让开墙线和炮位，留给工人施工')
        if cmd:
            return cmd
    if assignments is None:
        assignments = assign_weapons(state)
    weapon = assignments.get(role.id)
    if weapon:
        return move_on_path(
            state, role, weapon_approach_path(role, weapon, blocked, reserved, state),
            reserved, '开拓者白天不采矿不建墙，先去分配炮位',
        )
    return None


def pioneer_day_support(role, state, blocked, reserved, assignments):
    """开拓者白天合法工作：任务金币够了买武器券，用已有道具，让开施工，不去 collect/build。"""
    from .brain import decide_buy_medicine, decide_emergency_heal, decide_pioneer_voucher, decide_self_heal
    from .tactics import tactical_action
    heal = decide_emergency_heal(role, state)
    if heal:
        return selected(state, role.id, heal, '血量过低且近敌，紧急用药')
    cmd = decide_pioneer_voucher(role, state, blocked, reserved)
    if cmd:
        return cmd
    cmd = tactical_action(role, state, blocked, reserved)
    if cmd:
        return cmd
    heal = decide_self_heal(role)
    if heal:
        return selected(state, role.id, heal, '开拓者自救')
    buy = decide_buy_medicine(role, state)
    if buy:
        return buy
    return pioneer_stay_clear(role, state, blocked, reserved, assignments)


def replenish_walls(role, state, blocked, reserved, primary_only=False, allow_build=True):
    """缺墙就是持续施工任务，缺石主动找石矿，不转去采铜铁。仅工人：开拓者不能 collect/build。"""
    from .brain import own_station, try_build
    if role.role_type != 'worker':
        return False, None
    base = own_station(state)
    if base is None:
        return False, None
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    staged = staged_wall_plan(state, base)
    missing = set(staged) - existing
    if not missing:
        if primary_only:
            return False, None
        missing = set(active_wall_plan(state, base)) - existing
    if not missing:
        return False, None
    trace(state, role.id, 'persistent_wall_plan', '按阶段补墙，缺石就采石', missing=sorted(missing),
          wall_goal=len(staged), outer_unlocked=outer_wall_ready(state))
    if 'stone' in role.backpack:
        from .economy import clear_mine_target
        clear_mine_target(state, role.id)
        if not allow_build:
            trace(state, role.id, 'stones_reserved_for_late_day',
                  '正面已封，侧翼和外层留到回炮工时足够时再施工', stones=role.backpack.count('stone'), missing=len(missing))
            return False, None
        cmd = try_build(role, state, blocked, reserved)
        if cmd:
            state.policy_memory['wall_work_attempted'] = True
            return True, cmd
        return False, None
    from .economy import go_mine
    if len(role.backpack) < role.back_pack_capability:
        cmd = go_mine(
            role, state, blocked, reserved, want_ores=('stone',), purpose='stone',
            travel_reason='双层墙尚未完成，专程采石',
            collect_reason='采集下一段城墙所需石料',
        )
        if cmd:
            return True, cmd
    trace(state, role.id, 'wall_material_blocked', '缺墙但石矿不可达或背包已满', backpack_count=len(role.backpack))
    return False, None


def adjacent_critical_build(role, state, blocked, reserved):
    """操炮位旁补正面缺口：不离开炮去远工。"""
    from .brain import own_station
    if role.role_type != 'worker' or 'stone' not in role.backpack:
        return None
    base = own_station(state)
    if base is None:
        return None
    assignments = assign_weapons(state)
    for x, y in critical_wall_missing(state):
        if chebyshev(role.pos, Pos(x, y)) != 1:
            continue
        if (x, y) in blocked | reserved or (x, y, 'wall') in state.failed_build_spots:
            continue
        if not safe_wall(state, (x, y), blocked | reserved, assignments):
            continue
        reserved.add((x, y))
        return selected(state, role.id, {'action': 'build', 'name': 'wall', 'targetPos': [{'x': x, 'y': y}]},
                        '夜间空窗就近补正面墙，不离开操炮位')
    return None


def emergency_front_seal(role, state, blocked, reserved):
    """正面缺口会使关键目标暴露，且工人能在安全窗内封堵时，暂停未买到手的采购。"""
    from .tactics import threat_eta_to_base, threat_robots
    if role.role_type != 'worker' or 'stone' not in role.backpack:
        return None
    gaps = [Pos(*p) for p in critical_wall_missing(state)]
    if not gaps:
        return None
    near = [g for g in gaps if chebyshev(role.pos, g) <= 2]
    if not near:
        return None
    job = state.worker_item_jobs.get(role.id)
    if job and job.get('item') in role.backpack:
        return None
    travel = station_return_steps(role, state, blocked)
    arrival = threat_eta_to_base(state, role)
    if travel is None:
        return None
    if arrival is not None and 1 + travel + MUSTER_BUFFER >= arrival and not threat_robots(state):
        if min(chebyshev(role.pos, g) for g in near) > 1:
            return None
    target = min(near, key=lambda g: (chebyshev(role.pos, g), g.x, g.y))
    path = wall_approach_path(role, target, blocked | reserved, state)
    if path is None:
        retreat = interior_retreat_path(role, blocked | reserved, state)
        if not retreat:
            return None
        state.policy_memory['wall_work_attempted'] = True
        trace(state, role.id, 'emergency_front_seal', '正面紧急缺口，先回到院内再封堵')
        return move_on_path(state, role, retreat, reserved, '正面紧急缺口，先回到院内再封堵')
    if path:
        state.policy_memory['wall_work_attempted'] = True
        trace(state, role.id, 'emergency_front_seal', '正面紧急缺口，暂停非紧急采购先封堵')
        return move_on_path(state, role, path, reserved, '正面紧急缺口，先走到封堵位置')
    if (target.x, target.y) in blocked | reserved or (target.x, target.y, 'wall') in state.failed_build_spots:
        return None
    assignments = assign_weapons(state)
    if not safe_wall(state, (target.x, target.y), blocked | reserved, assignments):
        return None
    reserved.add((target.x, target.y))
    state.policy_memory['wall_work_attempted'] = True
    trace(state, role.id, 'emergency_front_seal', '正面紧急缺口，暂停非紧急采购先封堵')
    return selected(state, role.id, {'action': 'build', 'name': 'wall', 'targetPos': [{'x': target.x, 'y': target.y}]},
                    '正面紧急缺口，暂停非紧急采购先封堵')


def safe_wall(state, point, blocked, assignments):
    # 不把任何操控者封在无法返回其武器的位置；忽略可移动队友的临时占位。
    from .protocol import Role
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    actors = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')]
    obstacles = blocked - {(r.pos.x, r.pos.y) for r in actors}
    obstacles = obstacles | {point}
    yard = courtyard_cells(state, base) if base else set()

    def stationed(role):
        if not base or not yard or in_courtyard(state, base, role.pos):
            return role
        return Role(role.id, courtyard_anchor(state, base, obstacles), role.role_type, role.health)

    if not all(station_path(stationed(r), assignments[r.id], obstacles, state) is not None
               for r in actors if r.id in assignments):
        return False
    if yard:
        for role in actors:
            if (role.pos.x, role.pos.y) in yard:
                continue
            if path_to_any(role.pos, yard, obstacles, state.map_info.width, state.map_info.height) is None:
                return False
    # 同时保留原本可达的经济/任务目的地，不能只保证能回炮台。
    before = obstacles - {point}
    destinations = [z.pos for z in state.map_info.zones if z.neutral_type in ('vendor', 'weaponShop', 'stone')]
    for role in actors:
        targets = destinations if role.role_type == 'worker' else [t.task_position for t in state.team_our.player_tasks if t.is_valid]
        for target in targets:
            if adjacent_path(role, target, before, state) is not None and adjacent_path(role, target, obstacles, state) is None:
                return False
    return True


def plan_opening(state):
    from .brain import (
        WEAPON_TYPES, decide_emergency_heal, decide_self_heal, decide_shop_item_job, item_cost,
        is_day_round, maybe_start_shop_item_job, own_station, plan_pioneer_tasks, pick_weapon_name,
        release_stale_repair_job, should_upgrade_weapon,
    )
    from .economy import clear_mine_target, go_mine, liquidate, muster_for_night, profitable_mine
    from copy import copy
    if not is_day_round(state.round_no) and not first_night_economy_open(state):
        trace(state, None, 'opening_night_guard', '开局计划只在白天和首日入夜后到位点前运行',
              round_no=state.round_no, remaining=day_rounds_remaining(state.round_no),
              defense_remaining=defense_rounds_remaining(state))
        return {}
    base = own_station(state)
    if base is None:
        return {}
    fighters = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0]
    workers = sorted((r for r in fighters if r.role_type == 'worker'), key=lambda r: r.id)
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    ring = staged_wall_plan(state, base)
    existing_walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    missing = [p for p in ring if p not in existing_walls]
    blocked, reserved = build_blocked_set(state) | movement_avoid(state), set()
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    assignments = assign_weapons(state, excluded_ids=task_pioneers, persist=True)
    remaining = defense_rounds_remaining(state)
    travel = [weapon_approach_path(r, assignments[r.id], blocked, set(), state)
              for r in fighters if r.id in assignments]
    reachable = [len(p) for p in travel if p is not None]
    if travel and not reachable:
        muster_need = remaining + MUSTER_BUFFER
    else:
        muster_need = max(reachable + [0]) + MUSTER_BUFFER
    muster = bool(weapons) and remaining <= muster_need
    upgraded_count = sum(1 for w in weapons if (w.level or 1) >= 2)
    upgraded_once = upgraded_count >= 1
    has_three = len(weapons) >= 3
    gold, builds = state.team_our.gold_num, 0
    budget = opening_time_budget(state, missing, remaining, muster_need, gold, upgraded_count, blocked)
    if missing and state.policy_memory.get('opening_commit') == 'walls' and not budget.get('allow_mine'):
        budget['allow_mine'] = False
        budget['allow_sell'] = False
    if budget.get('allow_upgrade') and budget.get('allow_mine'):
        state.policy_memory.pop('opening_commit', None)
    allow_walls = has_three and budget['allow_walls']
    allow_upgrade = has_three and budget['allow_upgrade']
    allow_sell = has_three and budget['allow_sell']
    allow_mine = has_three and budget['allow_mine']
    if has_three and missing and allow_walls and not allow_mine and not allow_upgrade:
        state.policy_memory['opening_commit'] = 'walls'
    elif not missing or not has_three or (allow_upgrade and allow_mine):
        state.policy_memory.pop('opening_commit', None)
    if has_three:
        from .economy import pick_weapon_voucher_buyer
        from .pioneer_schedule import has_task_reservation
        buyer = pick_weapon_voucher_buyer(state, blocked)
        for role_id, job in list(state.worker_item_jobs.items()):
            owner = next((r for r in fighters if r.id == role_id), None)
            if (buyer and owner and buyer.id != owner.id and job.get('kind') == 'weapon'
                    and job.get('item') not in owner.backpack
                    and not (buyer.role_type == 'pioneer' and (
                        state.phase_task or has_task_reservation(state, buyer)))):
                state.worker_item_jobs[buyer.id] = job
                del state.worker_item_jobs[role_id]
                trace(state, buyer.id, 'weapon_upgrade_job_transferred',
                      '武器升级券改派给完整代价更低的人', from_id=role_id)
        if allow_upgrade:
            for extra in (False, True):
                if not should_upgrade_weapon(state):
                    break
                buyer = pick_weapon_voucher_buyer(state, blocked, extra=extra)
                if buyer and buyer.id not in state.worker_item_jobs:
                    maybe_start_shop_item_job(buyer, state)
    if muster:
        phase = '就位'
    elif not has_three:
        phase = '武器'
    elif allow_upgrade and not allow_walls:
        phase = '筹资升级'
    elif missing and allow_walls:
        phase = '围墙'
    elif allow_upgrade:
        phase = '筹资升级'
    else:
        phase = '就位'
    trace(state, None, 'opening_phase', '第一天阶段计划', phase=phase, weapons=len(weapons),
          wall_goal=len(ring), walls_completed=len(ring)-len(missing), wall_missing=missing,
            geometry_note='先三座火箭，再采铜铁升最多两门到2级（钱或回合不够则一门），迎敌墙约8段；格子合法性由执行反馈确认',
          rounds_to_night=day_rounds_remaining(state.round_no),
          rounds_to_defense=remaining,
          attack_from='右侧' if attack_direction(state, base) == 1 else '左侧', direction_source='用户确认的刷新规则')
    trace(state, None, 'opening_time_budget', '按完整升级链路判断继续升炮还是改去修墙',
          remaining=remaining, wall_need=budget['wall_need'], wall_deadline=budget['wall_deadline'],
          sell_trip=budget['sell_trip'], allow_walls=allow_walls, allow_upgrade=allow_upgrade,
          allow_sell=allow_sell, gold=gold, upgraded=upgraded_once, upgraded_count=upgraded_count,
          upgrade_status=budget.get('upgrade_status'), funding_deficit=budget.get('funding_deficit'),
          inventory_sale_value=budget.get('inventory_sale_value'), mine_rounds=budget.get('mine_rounds'),
          route_rounds=budget.get('route_rounds'), action_rounds=budget.get('action_rounds'),
          total_upgrade_rounds=budget.get('total_upgrade_rounds'), fallback_reason=budget.get('fallback_reason'))
    trace(state, None, 'funnel_layout', '实验性双层防线；外层留口，己方从后方通行',
          gap=funnel_gap(state, base), layers=2 if outer_wall_ready(state) and funnel_gap(state, base) else 1,
          outer_unlocked=outer_wall_ready(state),
          effect_note='机器人可能直接攻击墙，分流效果需回放验证')
    claimed = set()
    # 开拓者先规划撤离，避免继续占住墙线和工人施工邻接格。
    for role in sorted(fighters, key=lambda r: (r.role_type != 'pioneer', r.id)):
        if role.id in task_pioneers:
            continue
        heal = decide_emergency_heal(role, state)
        if heal:
            commands[role.id] = selected(state, role.id, heal, '低血紧急治疗')
            continue
        release_stale_repair_job(role, state)
        budget_state = copy(state)
        budget_state.team_our = copy(state.team_our)
        budget_state.team_our.gold_num = gold
        role_muster = bool(weapons) and defense_rounds_remaining(state, role) <= muster_need
        if has_three and role.role_type == 'worker':
            from .opening import emergency_front_seal
            seal = emergency_front_seal(role, state, blocked, reserved)
            if seal:
                commands[role.id] = seal
                continue
        if has_three and role.role_type == 'pioneer':
            from .brain import decide_pioneer_voucher
            cmd = decide_pioneer_voucher(role, budget_state, blocked, reserved)
            if cmd:
                if cmd['action'] == 'buy':
                    gold -= item_cost(cmd['name'], state)
                commands[role.id] = cmd
                continue
        handled, cmd = muster_for_night(role, state, blocked, reserved)
        if handled:
            if cmd:
                commands[role.id] = cmd
                continue
            from .economy import defense_occupancy
            occupancy, snap = defense_occupancy(role, state, blocked)
            if occupancy == 'must_hold':
                trace(state, role.id, 'mandatory_hold_no_action',
                      '必须留守或操炮，本回合不转去买券或采矿', occupancy=occupancy,
                      defenseDueReasons=snap.get('defenseDueReasons'),
                      threatEta=snap.get('threatEta'), travel=snap.get('travel'))
                continue
            trace(state, role.id, 'muster_empty_continue_economy',
                  '已到家但无强制留守，muster 空返回不阻断后续经济评估',
                  occupancy=occupancy, defenseDue=snap.get('defenseDue'),
                  defenseDueReasons=snap.get('defenseDueReasons'))
        trace(state, role.id, 'opening_rockets_first', '首日先三座火箭，再采铜铁升最多两门到2级，再补迎敌墙')
        if has_three:
            if allow_upgrade and role.role_type == 'worker':
                cmd = decide_shop_item_job(role, budget_state, blocked, reserved)
                if not cmd and should_upgrade_weapon(budget_state):
                    from .economy import worker_should_shop_weapon_voucher
                    if worker_should_shop_weapon_voucher(role, budget_state):
                        maybe_start_shop_item_job(role, budget_state)
                        cmd = decide_shop_item_job(role, budget_state, blocked, reserved)
                if cmd:
                    if cmd['action'] == 'buy':
                        gold -= item_cost(cmd['name'], state)
                    commands[role.id] = cmd
                    continue
                job = state.worker_item_jobs.get(role.id)
                if (job and job.get('kind') == 'weapon' and job.get('item') not in role.backpack
                        and gold >= item_cost(job['item'], state)):
                    other = next((w for w in workers if w.id != role.id and w.id not in state.worker_item_jobs), None)
                    if other:
                        state.worker_item_jobs[other.id] = job
                        del state.worker_item_jobs[role.id]
                        trace(state, role.id, 'weapon_upgrade_job_transferred',
                              '当前工人买不到券，转交给另一名工人', other_id=other.id)
            if not role_muster and allow_sell and any(z.neutral_type == 'vendor' for z in state.map_info.zones):
                handled, cmd = liquidate(role, budget_state, blocked, reserved)
                if cmd:
                    commands[role.id] = cmd
                    continue
            if not role_muster and allow_mine and not allow_walls and role.role_type == 'worker':
                cmd = go_mine(
                    role, budget_state, blocked, reserved, want_ores=('iron', 'copper'), purpose='voucher',
                    travel_reason='按小贩报价前往凑够升级券总回合最短的铜铁矿',
                    collect_reason='采集铜铁，凑够升级券再修墙',
                )
                if cmd:
                    commands[role.id] = cmd
                    continue
        if role_muster:
            weapon = assignments.get(role.id)
            if weapon:
                trace(state, role.id, 'weapon_assignment', '夜间一人一炮，提前就位', weapon_id=weapon.id)
                cmd = move_on_path(state, role, weapon_approach_path(role, weapon, blocked, reserved, state), reserved, '前往分配武器')
                if cmd:
                    commands[role.id] = cmd
            continue
        if role.role_type == 'pioneer':
            cmd = pioneer_day_support(role, state, blocked, reserved, assignments)
            if cmd:
                commands[role.id] = cmd
            continue
        if len(weapons) + builds < 3:
            if gold < 25:
                trace(state, role.id, 'opening_no_gold', '武器资金不足；三座火箭未齐前不改去修墙')
                continue
            pending = [c.get('name') for c in commands.values() if c.get('action') == 'build']
            weapon_name = pick_weapon_name(state, pending)
            candidates = weapon_candidates(state, base, weapon_name, pending, claimed)
            kind = 'weapon'
        elif len(weapons) < 3:
            trace(state, role.id, 'await_weapons', '等待本回合武器建造结果，不提前转入围墙')
            continue
        elif missing and allow_walls:
            kind, candidates = 'wall', missing
            stones = role.backpack.count('stone')
            at_stone = any(z.neutral_type == 'stone' and chebyshev(role.pos, z.pos) <= 1 for z in state.map_info.zones)
            if stones == 0 or (at_stone and stones < min(STONE_BATCH, (len(missing)+1)//2) and remaining > 12):
                cmd = go_mine(
                    role, state, blocked, reserved, want_ores=('stone',), purpose='stone',
                    travel_reason='前往可达石矿准备建墙材料',
                    collect_reason='为连续建墙批量采石',
                )
                if cmd:
                    commands[role.id] = cmd
                    continue
            if stones == 0:
                trace(state, role.id, 'wall_no_stone', '没有石头，且没有可执行的采石行动')
                continue
            clear_mine_target(state, role.id)
        else:
            if upgraded_count >= DAY1_WEAPON_L2_TARGET and role.role_type == 'worker':
                cmd = profitable_mine(role, budget_state, blocked, reserved)
                if cmd:
                    commands[role.id] = cmd
            continue
        if kind == 'wall':
            sticky = tuple(state.policy_memory.get('opening_wall_targets', {}).get(str(role.id), ()))
            occupied = (blocked | reserved | claimed) - {(role.pos.x, role.pos.y)}

            def wall_sort_key(point):
                avoid = set(candidates) - {point}
                path = wall_approach_path(role, Pos(*point), blocked | reserved, state, extra_avoid=avoid)
                if path is None:
                    path = wall_approach_path(role, Pos(*point), blocked | reserved, state)
                unreachable = path is None
                can_build = path != []
                return (unreachable, can_build, 0 if point == sticky else 1,
                        wall_priority(state, base, point), 0 if path is None else len(path), point)

            candidates = sorted(candidates, key=wall_sort_key)
        for point in candidates:
            occupied = (blocked | reserved | claimed) - {(role.pos.x, role.pos.y)}
            if point in occupied or (*point, kind) in state.failed_build_spots:
                continue
            if kind == 'wall' and not safe_wall(state, point, blocked | claimed, assignments):
                continue
            if kind == 'wall':
                avoid = set(candidates) - {point}
                path = wall_approach_path(role, Pos(*point), blocked | reserved, state, extra_avoid=avoid)
                if path is None:
                    path = wall_approach_path(role, Pos(*point), blocked | reserved, state)
            else:
                path = adjacent_path(role, Pos(*point), blocked | reserved, state)
            if path is None:
                continue
            claimed.add(point)
            if kind == 'wall':
                state.policy_memory.setdefault('opening_wall_targets', {})[str(role.id)] = list(point)
            if kind == 'weapon':
                builds += 1
            if path:
                cmd = move_on_path(state, role, path, reserved, '前往武器施工位' if kind == 'weapon' else '从院内接近迎敌墙缺口')
            else:
                name = pick_weapon_name(state, [c.get('name') for c in commands.values() if c.get('action') == 'build']) if kind == 'weapon' else 'wall'
                cmd = selected(state, role.id, {'action': 'build', 'name': name, 'targetPos': [{'x': point[0], 'y': point[1]}]}, '建造武器' if kind == 'weapon' else '建造迎敌防线')
                reserved.add(point)
                if kind == 'weapon':
                    gold -= 25
                else:
                    blocked.add(point)
                    state.policy_memory.get('opening_wall_targets', {}).pop(str(role.id), None)
            if cmd:
                commands[role.id] = cmd
            break
        else:
            trace(state, role.id, 'opening_no_candidate', '候选位置被占用、不可达、处于失败冷却或会封住返程；未完成墙线不会标为完成', kind=kind)
            if kind == 'wall':
                path = interior_retreat_path(role, (blocked | reserved) - {(role.pos.x, role.pos.y)}, state)
                cmd = move_on_path(state, role, path, reserved, '先回到院内，再从内侧封闭缺口')
                if cmd:
                    commands[role.id] = cmd
                    continue
            weapon = assignments.get(role.id)
            if weapon:
                cmd = move_on_path(state, role, weapon_approach_path(role, weapon, blocked, reserved, state), reserved, '暂无施工位，先去分配武器避免空转')
                if cmd:
                    commands[role.id] = cmd
    for role in fighters:
        if role.id not in commands:
            heal = decide_self_heal(role)
            if heal:
                commands[role.id] = selected(state, role.id, heal, '没有更高优先级行动，最后执行自救')
                continue
            weapon = assignments.get(role.id)
            if weapon:
                cmd = move_on_path(state, role, weapon_approach_path(role, weapon, blocked, reserved, state), reserved, '没有施工指令则先守炮，避免空转')
                if cmd:
                    commands[role.id] = cmd
    return commands
