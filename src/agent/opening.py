"""第一天：五阶段状态机 BUILD_WEAPONS → FUND_FIRST_UPGRADE → APPLY_FIRST_UPGRADE → BUILD_SURVIVAL_WALL → MUSTER。

墙线是候选几何规划，不是官方合法区域；以快照中的建筑判断完成。
"""
from collections import deque
from itertools import combinations, permutations

from .protocol import Pos
from .grid import build_blocked_set, chebyshev, neighbors8
from .decision_log import trace, selected

WALL_MARGIN = 2
STONE_BATCH = 6  # 墙阶段两名工人各备半圈，减少往返。
MUSTER_BUFFER = 3
# 75/76 只是观测到的开火时点，不再推迟官方入夜（cycle 70）后的远程经济。
DAY1_L2_GUNNER_READY_CYCLE = 75
DAY1_OTHER_READY_CYCLE = 76
DAY1_WALL_TARGET = 8
DAY2_WALL_TARGET = 12
REQUIRED_OPENING_UPGRADES = 1  # 第一门 2 级后主目标完成，立即修墙。
OPTIONAL_PARALLEL_UPGRADES = 1  # 第二门只用现金/余券并行，不关墙。
DAY1_WEAPON_L2_TARGET = REQUIRED_OPENING_UPGRADES + OPTIONAL_PARALLEL_UPGRADES
OPENING_METAL_BATCH = 15  # 开局攒够一批铜铁再卖，不采几块就跑小贩。
WALL_STEP_SLACK = 1    # 每段墙在建造外再留1回合走位。
LATE_BUILD_SLACK = 2   # 墙工时 overrun 的初值，随后按入夜时是否仍缺墙调整。
MAX_WALL_OVERRUN = 12
SURVIVAL_WALL_FLOOR = 6
JOB_STALL_ROUNDS = 3
OPENING_COMMIT_SURVIVAL = 'survival_walls'


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


NIGHT_ROBOT_AVOID_RADIUS = 2  # 夜里绕开机器人周围这么多格。


def night_danger_cells(state, include_front=True):
    """夜里要绕开的格子：防线正面以外（机器人来的方向）和机器人周围。"""
    from .brain import own_station
    from .tactics import threat_robots
    width, height = state.map_info.width, state.map_info.height
    cells = set()
    r = NIGHT_ROBOT_AVOID_RADIUS
    base = own_station(state)
    yard = courtyard_cells(state, base) if base is not None else set()
    for robot in threat_robots(state):
        cells.update((x, y) for x in range(robot.pos.x - r, robot.pos.x + r + 1)
                     for y in range(robot.pos.y - r, robot.pos.y + r + 1))
        if base is not None:
            # 机器人朝基地推进的路线（每步 x、y 各向基地靠一格），两侧各留一格，院子里不算。
            x, y = robot.pos.x, robot.pos.y
            for _ in range(width + height):
                cells.update((cx, cy) for cx in (x - 1, x, x + 1) for cy in (y - 1, y, y + 1)
                             if (cx, cy) not in yard)
                if chebyshev(Pos(x, y), base.pos) <= 1:
                    break
                x += (base.pos.x > x) - (base.pos.x < x)
                y += (base.pos.y > y) - (base.pos.y < y)
    if include_front and base is not None:
        left, right, _, _ = defense_bounds(state, base)
        direction = attack_direction(state, base)
        front = right if direction == 1 else left
        cells.update((x, y) for x in range(width) for y in range(height) if (x - front) * direction > 0)
    return cells


def night_strict_path(role, target, blocked, state):
    """夜里完全避开正面和机器人的路径；没有就返回 None，不退化。"""
    here = {(role.pos.x, role.pos.y)}
    return adjacent_path(role, target, set(blocked) | (night_danger_cells(state) - here), state)


def courtyard_path(role, target, blocked, state):
    """只在院子里走到目标邻格（夜里在家修墙用）；人不在院子里或够不着返回 None。"""
    from .brain import own_station
    base = own_station(state)
    if base is None:
        return None
    allowed = courtyard_cells(state, base) | {(role.pos.x, role.pos.y)}
    width, height = state.map_info.width, state.map_info.height
    outside = {(x, y) for x in range(width) for y in range(height)} - allowed
    return adjacent_path(role, target, set(blocked) | outside, state)


def night_safe_path(role, target, blocked, state):
    """夜里从基地后方绕行：先同时避开正面和机器人，走不通只避机器人，再不行走普通路径。白天就是普通路径。"""
    from .brain import is_day_round
    if is_day_round(state.round_no):
        return adjacent_path(role, target, blocked, state)
    if role.id in (getattr(state, 'night_released_ids', None) or ()):
        # 夜里被放出去的人只走完全避开正面和机器人的路线，找不到就不去。
        return night_strict_path(role, target, blocked, state)
    here = {(role.pos.x, role.pos.y)}
    for include_front in (True, False):
        avoid = night_danger_cells(state, include_front=include_front) - here
        path = adjacent_path(role, target, set(blocked) | avoid, state)
        if path is not None:
            return path
    return adjacent_path(role, target, blocked, state)


def courtyard_cells(state, base):
    left, right, bottom, top = defense_bounds(state, base)
    return {(x, y) for x in range(left + 1, right) for y in range(bottom + 1, top)}


def in_courtyard(state, base, pos):
    return (pos.x, pos.y) in courtyard_cells(state, base)


def rear_gate_cells(state, base):
    """U 形开口在后方竖边：院子里最靠后的一列，进出都走这里，不贴迎敌面绕。"""
    left, right, bottom, top = defense_bounds(state, base)
    gate_x = left + 1 if attack_direction(state, base) == 1 else right - 1
    return {(gate_x, y) for y in range(bottom + 1, top)}


def attack_side_of_front(state, base, pos):
    """格子是否在迎敌墙的外侧（敌人来的那一侧）。"""
    left, right, _, _ = defense_bounds(state, base)
    direction = attack_direction(state, base)
    front = right if direction == 1 else left
    return (pos.x - front) * direction > 0


def courtyard_anchor(state, base, blocked):
    cells = sorted(courtyard_cells(state, base),
                   key=lambda p: (max(abs(p[0] - base.pos.x), abs(p[1] - base.pos.y)), p))
    for cell in cells:
        if cell not in blocked:
            return Pos(*cell)
    return Pos(base.pos.x, base.pos.y)


def failed_move_cells(state, role):
    """上一回合失败的移动落点，本回合寻路绕开，避免对着同一格空转。"""
    cells = set()
    stored = (state.policy_memory.get('failed_move_cells') or {}).get(str(role.id)) or []
    for item in stored:
        try:
            cells.add((int(item[0]), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    prev = (state.last_sent_command or {}).get(role.id) or {}
    if prev.get('action') == 'move' and (state.last_round_role_action_results or {}).get(role.id) is False:
        tp = prev.get('targetPos') or [{}]
        try:
            cells.add((int(tp[0]['x']), int(tp[0]['y'])))
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    here = (role.pos.x, role.pos.y)
    cells.discard(here)
    mem = state.policy_memory.setdefault('failed_move_cells', {})
    mem[str(role.id)] = [list(c) for c in list(cells)[-8:]]
    return cells


def step_into_courtyard(role, blocked, state, toward=None):
    """人在墙线/墙外但已经贴着院子时，一步迈进院子，不要沿着墙格走。

    有施工目标时迈向目标，避免总是走进坐标最小的后方格，和出院路径对着晃。
    """
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None or not state.map_info:
        return None
    yard = courtyard_cells(state, base)
    here = (role.pos.x, role.pos.y)
    if here in yard:
        return []
    obstacles = set(blocked) - {here}
    options = [cell for cell in neighbors8(role.pos, state.map_info.width, state.map_info.height)
               if (cell.x, cell.y) in yard and (cell.x, cell.y) not in obstacles]
    if not options:
        return None
    if toward is not None:
        return [min(options, key=lambda p: (chebyshev(p, toward), p.x, p.y))]
    return [min(options, key=lambda p: (p.x, p.y))]


def step_off_construction(role, state, blocked, reserved):
    """站在施工格上时优先迈进院子，避免迈到墙外再绕回来。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if not state.map_info:
        return None
    yard = courtyard_cells(state, base) if base else set()
    ring = set(wall_ring(state, base)) if base else set()
    options = []
    for step in neighbors8(role.pos, state.map_info.width, state.map_info.height):
        key = (step.x, step.y)
        if key in blocked | reserved:
            continue
        attack = bool(base and attack_side_of_front(state, base, step))
        rank = (0 if key in yard else 1, 1 if key in ring else 0, 1 if attack else 0, key)
        options.append((rank, key, step))
    if not options:
        return None
    _rank, key, step = min(options)
    reserved.add(key)
    return selected(state, role.id, {'action': 'move', 'targetPos': [{'x': step.x, 'y': step.y}]},
                    '先离开施工格再建造')


def _path_wraps_attack_front(state, base, path):
    """贴迎敌墙外侧横向绕行（不是直线穿缺口）。偏一格避施工点不算绕行。"""
    if not path:
        return False
    outside = [p for p in path if attack_side_of_front(state, base, p)]
    if len(outside) <= 2:
        return False
    ys = [p.y for p in outside]
    return max(ys) - min(ys) >= 2


def enter_courtyard_path(role, obstacles, state, toward=None):
    """墙外进院：贴院一步迈进；未砌墙格可走；贴迎敌墙外侧横向绕行时改走后方开口。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None:
        return None
    into = step_into_courtyard(role, obstacles, state, toward=toward)
    if into:
        return into
    yard = courtyard_cells(state, base)
    if not yard:
        return None
    enter = path_to_any(role.pos, yard, obstacles, state.map_info.width, state.map_info.height)
    retreat = rear_retreat_path(role, obstacles, state)
    if attack_side_of_front(state, base, role.pos):
        if retreat and (enter is None or _path_wraps_attack_front(state, base, enter)):
            return retreat
        if enter is not None:
            return enter
        return retreat
    if enter is not None:
        return enter
    return retreat


def _outside_stay_blockers(stay, width, height, here):
    return {(x, y) for x in range(width) for y in range(height) if (x, y) not in stay} - {here}


def wall_approach_path(role, target, blocked, state, extra_avoid=()):
    """从院子内侧接近墙。已经贴着施工格就地建造；在院内不许出院绕行。

    未砌的墙格不能当障碍：否则工人会被逼着贴迎敌墙外侧绕完整条墙线。
    也不再优先走「完全避开墙线」的远路——那会把人送到后沿再绕回来。
    """
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None:
        return adjacent_path(role, target, blocked, state)
    if chebyshev(role.pos, target) == 1 and not attack_side_of_front(state, base, role.pos):
        return []
    yard = courtyard_cells(state, base)
    here = (role.pos.x, role.pos.y)
    width, height = state.map_info.width, state.map_info.height
    obstacles = (mobile_walkable(state, set(blocked) | set(extra_avoid)) | failed_move_cells(state, role)) - {here}
    if yard and here not in yard:
        enter = enter_courtyard_path(role, obstacles, state, toward=target)
        if enter is not None:
            return enter
    obstacles.add((target.x, target.y))
    direction = attack_direction(state, base)
    wall_dist = chebyshev(target, Pos(base.pos.x, base.pos.y))
    goals = set()
    for cell in neighbors8(target, width, height):
        key = (cell.x, cell.y)
        if key in obstacles and cell != role.pos:
            continue
        if yard and here in yard and key not in yard:
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
    if here in yard:
        interior = obstacles | _outside_stay_blockers(yard | {here}, width, height, here)
        return path_to_any(role.pos, goals, interior, width, height)
    return path_to_any(role.pos, goals, obstacles, width, height)


def rear_retreat_path(role, blocked, state):
    """墙外回院：目标是后方开口，不贴迎敌墙外侧绕半圈。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None:
        return None
    yard = courtyard_cells(state, base)
    if not yard:
        return None
    if (role.pos.x, role.pos.y) in yard:
        return []
    gates = {p for p in rear_gate_cells(state, base)
             if p not in blocked or p == (role.pos.x, role.pos.y)}
    if gates:
        path = path_to_any(role.pos, gates, blocked, state.map_info.width, state.map_info.height)
        if path is not None:
            return path
    return path_to_any(role.pos, yard, blocked, state.map_info.width, state.map_info.height)


def interior_retreat_path(role, blocked, state):
    """墙外空转时先回到院内，而不是贴着外墙绕圈。"""
    return rear_retreat_path(role, blocked, state)


def yard_exit_cells(role, state, blocked):
    """院内的人走出院子要经过的格（不含起点，含跨出的墙线缺口）。队友当作会让开。
    夜里放人外出时把这些格留给他，其他人回炮不要先站上去把他堵在院里。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    if base is None or not state.map_info or not in_courtyard(state, base, role.pos):
        return set()
    left, right, bottom, top = defense_bounds(state, base)
    yard = courtyard_cells(state, base)
    border = {(x, y) for x in range(left, right + 1) for y in range(bottom, top + 1)} - yard
    obstacles = mobile_walkable(state, blocked) - {(role.pos.x, role.pos.y)}
    goals = border - obstacles
    path = path_to_any(role.pos, goals, obstacles, state.map_info.width, state.map_info.height)
    return {(p.x, p.y) for p in path or []}


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
    """0=迎敌正面一列，2=两侧翼。规则不允许双层墙，只有一层。"""
    left, right, _, _ = defense_bounds(state, base)
    front = right if attack_direction(state, base) == 1 else left
    return 0 if point[0] == front else 2


def weapon_slot_plan(state, base):
    """武器编位：前排两侧一火箭一电磁，火箭侧后方再补一门火箭。"""
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    front_x = (right if direction == 1 else left) - direction
    rear_x = front_x - direction
    y_low, y_high = bottom + 1, top - 1
    width, height = state.map_info.width, state.map_info.height
    slots = [
        ('rocket', (front_x, y_low)),
        ('railgun', (front_x, y_high)),
        ('rocket', (rear_x, y_low)),
    ]
    station = {(base.pos.x + dx, base.pos.y - dy) for dx in (0, 1) for dy in (0, 1)}
    cleaned = []
    for name, (x, y) in slots:
        if not (0 <= x < width and 0 <= y < height) or (x, y) in station:
            continue
        cleaned.append((name, (x, y)))
    return cleaned


def weapon_slots(state, base):
    return [point for _name, point in weapon_slot_plan(state, base)]


def wall_ring(state, base):
    """单层防线：迎敌正面一整列，两翼一直延伸到院子后沿，后方竖边开放。
    顺序：正面 → 两翼从靠前往后交替展开，后面的墙最后修。"""
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    front = right if direction == 1 else left
    cells = {(front, y) for y in range(bottom, top + 1)}
    cells.update((x, y) for x in range(left, right + 1) for y in (bottom, top))
    return sorted(cells, key=lambda p: (wall_priority(state, base, p), abs(p[0] - front), p[1]))


def primary_wall_plan(state, base):
    return wall_ring(state, base)


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


def wall_finish_rounds(state, missing, blocked, hands=None):
    """两名工人补完当前阶段墙的回合下界：缺石采集 + 走到缺口 + 每段建造。"""
    n = len(missing)
    if n <= 0:
        return 0
    workers = [r for r in state.team_our.roles if r.role_type == 'worker' and r.health > 0]
    if hands is None:
        hands = max(1, len(workers))
    else:
        hands = max(1, int(hands))
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
    """夜防到位按官方入夜 cycle 70。75/76 只作观测注释，不推迟回防。"""
    from .brain import DAY_ROUNDS
    return DAY_ROUNDS


def defense_rounds_remaining(state, role=None):
    """距官方入夜的剩余回合；夜间为 0。"""
    return day_rounds_remaining(state.round_no)


def first_night_economy_open(state):
    """官方入夜后不再走开局远程经济。保留函数名给旧调用。"""
    return False


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
        from .economy import (
            opening_cashout_owner, team_metal_inventory_value, worker_has_metal, worker_metal_count,
        )
        team_inv = team_metal_inventory_value(state)
        inventory_value = team_inv
        remaining_value = max(0, deficit - team_inv)
        prices_unknown = not any(prices.get(n, 0) > 0 for n in ('iron', 'copper'))
        team_has_metal = any(
            r.role_type == 'worker' and r.health > 0 and worker_has_metal(r, state)
            for r in (state.team_our.roles if state.team_our else [])
        )
        if prices_unknown and remaining_value > 0 and not team_has_metal:
            return _blank_upgrade_estimate(
                status='sale_value_unknown', funding_deficit=deficit, inventory_sale_value=0,
                fallback_reason='metal_price_unknown', actor_id=role.id,
            )
        need_mine = remaining_value > 0 and not prices_unknown
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
        owner_id = opening_cashout_owner(state)
        seller = next((r for r in (state.team_our.roles or []) if r.id == owner_id), role) if owner_id else role
        if seller.id != role.id and worker_metal_count(seller, state) > 0:
            walked = _walk_adjacent(seller, vendor.pos, blocked, state)
            if walked is None:
                return _blank_upgrade_estimate(
                    status='need_mine' if mine_rounds else 'sell_inventory',
                    funding_deficit=deficit, inventory_sale_value=inventory_value,
                    mine_rounds=mine_rounds, route_rounds=route_rounds,
                    fallback_reason='vendor_unreachable', actor_id=role.id, ore=ore,
                )
            steps, _seller_actor = walked
            route_rounds += steps
            action_rounds += max(1, len([n for n in ('iron', 'copper') if n in seller.backpack]))
        else:
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
            action_rounds += max(1, len(metals) or 1)
        if prices_unknown:
            status = 'sale_value_unknown'
            inventory_value = 0
        else:
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
    est = _finish_upgrade_estimate(
        role, actor, blocked, state, status, deficit, inventory_value,
        mine_rounds, route_rounds, action_rounds, ore=ore,
    )
    if status == 'sale_value_unknown':
        est['fallback_reason'] = 'metal_price_unknown'
        est['inventory_sale_value'] = 0
    return est


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


def opening_has_voucher(state):
    return any(
        'WeaponUpgradeVoucher1' in (r.backpack or [])
        for r in (state.team_our.roles if state.team_our else [])
        if r.role_type in ('worker', 'pioneer') and r.health > 0
    )


def survival_walls_locked(state):
    return (state.policy_memory or {}).get('opening_commit') in (OPENING_COMMIT_SURVIVAL, 'walls')


def clear_opening_commit_after_first_night(state):
    """首夜结束后才解除生存墙承诺。"""
    if (state.round_no or 0) >= 130 and (state.round_no or 0) % 130 == 0:
        (state.policy_memory or {}).pop('opening_commit', None)
        (state.policy_memory or {}).pop('cashout_pending', None)
        (state.policy_memory or {}).pop('opening_job_progress', None)


def _opening_zone_reachable(state, neutral_type, blocked=None):
    """True/False 表示可达；None 表示快照里没有该中立点，不按不可达锁墙。"""
    from .brain import find_zone
    from .grid import build_blocked_set
    zone = find_zone(state, neutral_type)
    if zone is None or state.map_info is None:
        return None
    if blocked is None:
        blocked = build_blocked_set(state)
    actors = [r for r in (state.team_our.roles if state.team_our else [])
              if r.role_type in ('worker', 'pioneer') and r.health > 0]
    return any(adjacent_path(role, zone.pos, blocked, state) is not None for role in actors)


def opening_upgrade_funding(state, gold=None, blocked=None):
    """第一张券是否已经资金闭环。继续采矿不算闭环。"""
    from .brain import item_cost
    from .economy import ore_prices, team_metal_inventory_value, worker_has_metal
    cost = item_cost('WeaponUpgradeVoucher1', state)
    gold = 0 if gold is None and not state.team_our else (
        gold if gold is not None else state.team_our.gold_num)
    inventory_value = team_metal_inventory_value(state)
    info = {
        'funded': False, 'reason': 'unfunded', 'pending': False, 'cost': cost,
        'gold': gold, 'inventory_value': inventory_value,
    }
    if opening_has_voucher(state):
        info.update(funded=True, reason='have_voucher')
        return info
    if gold >= cost:
        if _opening_zone_reachable(state, 'weaponShop', blocked) is False:
            info['reason'] = 'shop_unreachable'
            return info
        info.update(funded=True, reason='gold_ready')
        return info
    prices = ore_prices(state)
    known = any(prices.get(name, 0) > 0 for name in ('iron', 'copper'))
    has_metal = any(
        r.role_type == 'worker' and r.health > 0 and worker_has_metal(r, state)
        for r in (state.team_our.roles if state.team_our else [])
    )
    vendor_ok = _opening_zone_reachable(state, 'vendor', blocked)
    shop_ok = _opening_zone_reachable(state, 'weaponShop', blocked)
    if known and inventory_value > 0 and gold + inventory_value >= cost:
        if vendor_ok is False:
            info['reason'] = 'vendor_unreachable'
            return info
        if shop_ok is False:
            info['reason'] = 'shop_unreachable'
            return info
        info.update(funded=True, reason='inventory_covers')
        return info
    if not known and has_metal:
        if vendor_ok is False:
            info['reason'] = 'vendor_unreachable'
            return info
        info.update(reason='cashout_pending', pending=True)
        return info
    if vendor_ok is False and has_metal:
        info['reason'] = 'vendor_unreachable'
        return info
    if shop_ok is False:
        info['reason'] = 'shop_unreachable'
        return info
    return info


def lock_survival_walls(state, reason):
    prev = (state.policy_memory or {}).get('opening_commit')
    state.policy_memory['opening_commit'] = OPENING_COMMIT_SURVIVAL
    state.policy_memory.pop('cashout_pending', None)
    if prev != OPENING_COMMIT_SURVIVAL:
        release_unbought_opening_weapon_jobs(state, reason)
        trace(state, None, 'survival_wall_lock', '升级资金未闭环，锁定首日最低生存墙',
              fallback_reason=reason, previous_commit=prev)
    return reason


def survival_wall_plan(state, base):
    """第一天主目标：正面整列 + 两侧拐角再各延伸 1 格。其余 16 段留给后面有余量再补。"""
    from .brain import WEAPON_TYPES
    inner = [p for p in primary_wall_plan(state, base) if wall_priority(state, base, p) != 1]
    front = [p for p in inner if wall_priority(state, base, p) == 0]
    flanks = [p for p in inner if wall_priority(state, base, p) == 2]
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    fighters = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0]

    def front_key(point):
        _x, y = point
        cover_base = 0 if abs(y - base.pos.y) <= 2 else 1
        cover_weapon = 0 if any(abs(y - w.pos.y) <= 1 for w in weapons) else 1
        cover_gunner = 0 if any(abs(y - f.pos.y) <= 1 and chebyshev(f.pos, Pos(*point)) <= 3 for f in fighters) else 1
        return (cover_base, cover_weapon, cover_gunner, abs(y - base.pos.y), y)

    ordered = sorted(front, key=front_key)
    # 两翼各取紧挨正面的那一格（flanks 已按由前往后排序），不取后沿。
    for row in sorted({p[1] for p in flanks}):
        point = next(p for p in flanks if p[1] == row)
        if point not in ordered:
            ordered.append(point)
    if len(ordered) < SURVIVAL_WALL_FLOOR:
        for point in inner:
            if point not in ordered:
                ordered.append(point)
            if len(ordered) >= SURVIVAL_WALL_FLOOR:
                break
    return ordered


def survival_wall_missing(state):
    from .brain import own_station
    base = own_station(state)
    if base is None:
        return []
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    return [p for p in survival_wall_plan(state, base) if p not in existing]


def extra_wall_missing(state):
    """生存墙以外、16 段里还缺的侧翼/后沿。"""
    from .brain import own_station
    base = own_station(state)
    if base is None:
        return []
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    core = set(survival_wall_plan(state, base))
    return [p for p in primary_wall_plan(state, base) if p not in existing and p not in core]


def opening_wall_work_list(state, survival_mode):
    surviving = survival_wall_missing(state)
    if surviving:
        return surviving
    staged = staged_wall_missing(state)
    if survival_mode:
        from .brain import own_station
        base = own_station(state)
        if base is None:
            return []
        return [p for p in staged if wall_priority(state, base, p) != 1]
    return staged


def apply_survival_budget(budget, reason, allow_voucher_use=False, allow_gold_upgrade=False):
    can_upgrade = bool(allow_voucher_use or allow_gold_upgrade)
    budget.update(
        allow_walls=True, allow_upgrade=can_upgrade, allow_sell=False, allow_mine=False,
        allow_income_mine=False, allow_stone_mine=True, allow_first_upgrade=can_upgrade,
        upgrade_safe=False, opening_phase='SURVIVAL_WALL',
        fallback_reason=reason or budget.get('fallback_reason') or 'upgrade_unfunded_survival_first',
    )
    if not allow_gold_upgrade and not allow_voucher_use:
        budget['upgrade_funded'] = False
    return budget


def day1_second_upgrade_fits(state, blocked=None):
    """首日第二门：只用已有现金或已持券，且买用链路不挤掉修墙。不为此再去采矿。"""
    from .brain import item_cost
    from .grid import build_blocked_set
    if (state.round_no or 0) // 130 > 0:
        return False
    if live_l2_weapon_count(state) < REQUIRED_OPENING_UPGRADES:
        return False
    if blocked is None:
        blocked = build_blocked_set(state)
    cost = item_cost('WeaponUpgradeVoucher1', state)
    has_voucher = any(
        'WeaponUpgradeVoucher1' in r.backpack
        for r in (state.team_our.roles if state.team_our else [])
        if r.role_type in ('worker', 'pioneer') and r.health > 0
    )
    gold = (state.team_our.gold_num if state.team_our else 0) - reserved_unbought_weapon_gold(state)
    if not has_voucher and gold < cost:
        return False
    remaining = day_rounds_remaining(state.round_no)
    est = estimate_opening_upgrade(state, blocked, gold=max(0, gold))
    total = est.get('total')
    if not est.get('ok') or total is None:
        return has_voucher or gold >= cost
    return remaining > total + MUSTER_BUFFER


def opening_time_budget(state, missing, remaining, muster_need, gold, upgraded_once, blocked):
    """由 opening_stage 派生允许项，不再每回合用估算在筹资和修墙之间切换。"""
    from .brain import WEAPON_TYPES, item_cost
    from .opening_schedule import flags_from_opening_stage, past_first_upgrade_cutoff, resolve_opening_stage
    stage = resolve_opening_stage(state, remaining=remaining, muster_need=muster_need, gold=gold)
    cost = item_cost('WeaponUpgradeVoucher1', state)
    has_voucher = opening_has_voucher(state)
    flags = flags_from_opening_stage(stage, gold, cost, has_voucher, cutoff=past_first_upgrade_cutoff(state, remaining))
    survival_missing = survival_wall_missing(state)
    work = missing if missing else survival_missing
    wall_need = wall_finish_rounds(state, work, blocked)
    upgraded_count = int(upgraded_once) if isinstance(upgraded_once, bool) else int(upgraded_once or 0)
    live_weapons = [r for r in (state.team_our.roles if state.team_our else [])
                    if r.role_type in WEAPON_TYPES and r.health > 0]
    budget = {
        'wall_need': wall_need, 'wall_deadline': wall_need + muster_need, 'sell_trip': None,
        'can_finish_walls': remaining > wall_need + muster_need,
        'can_finish_critical': True,
        'can_finish_survival_walls': remaining > wall_need + muster_need,
        'critical_deadline': wall_need + muster_need, 'survival_deadline': wall_need + muster_need,
        'survival_need': wall_need,
        'upgrade_status': flags.get('funding_reason'),
        'funding_deficit': max(0, cost - gold),
        'inventory_sale_value': 0,
        'mine_rounds': 0, 'route_rounds': 0, 'action_rounds': 0,
        'total_upgrade_rounds': None,
        'upgraded_count': upgraded_count if live_weapons else 0,
        'required_opening_upgrades': REQUIRED_OPENING_UPGRADES,
        'opening_stage': stage,
    }
    budget.update(flags)
    budget['required_done'] = upgraded_count >= REQUIRED_OPENING_UPGRADES
    return budget


def staged_wall_plan(state, base):
    """当前墙目标：按动态防线需求裁剪，不用 8/12 之类的常数截断当天墙数。

    DAY1_WALL_TARGET 只是"第一批关键墙"的批次大小；只要资源、路径和夜前工时允许，
    第一天可以继续把墙线扩到 10 段以上。
    """
    from .work_orders import wall_feasible_target
    plan = primary_wall_plan(state, base)
    if day_index(state) >= 2:
        return plan
    return plan[:max(0, wall_feasible_target(state))]


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
    # 这是回炮耗时估算：队友会走开，不把他们此刻的位置当成永久障碍，
    # 否则队友站在唯一操炮位上时，开拓者会整天被判定“回不去、必须回防”而原地打转。
    blocked = set(blocked) - {
        (r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')
    }
    path = station_path(actor, weapon, blocked, state)
    if path is None:
        # 分配炮位暂时站不进去时，按最近的其它武器估算回防时间，不因此拒掉全部任务。
        others = [r for r in state.team_our.roles
                  if r.role_type in ('gatling', 'railgun', 'rocket') and r.health > 0 and r.id != weapon.id]
        paths = [(len(p), w.id, p, w) for w in others
                 for p in [station_path(actor, w, blocked, state)] if p is not None]
        if paths:
            _steps, _wid, path, weapon = min(paths)
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
    blocked = build_blocked_set(state)
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


def worker_one_wall_rounds(state, role, missing=None):
    """建完离自己最近的一段墙并回炮的估计。missing 必须由调用方给出，避免和 due_wall_gaps 互相调用。"""
    if missing is None:
        missing = extra_wall_missing(state) or survival_wall_missing(state)
    if not missing:
        return 0
    if role is None:
        return worker_wall_muster_rounds(state, None, missing[:1])
    blocked = build_blocked_set(state)
    nearest = None
    best = None
    for point in missing:
        path = wall_approach_path(role, Pos(*point), blocked, state)
        if path is None:
            continue
        cost = len(path)
        if best is None or cost < best:
            best = cost
            nearest = point
    return worker_wall_muster_rounds(state, role, [nearest] if nearest else missing[:1])


def can_extend_walls(state, role=None):
    """夜前还能再建一段并回炮，才补生存墙以外的段。第一天由开局状态机管截止。"""
    from .tactics import night_wave_cleared
    extra = extra_wall_missing(state)
    if not extra:
        return False
    if night_wave_cleared(state) or (state.round_no or 0) < 70:
        return True
    remaining = defense_rounds_remaining(state, role)
    if remaining <= 0:
        return False
    if role is not None and 'stone' in (role.backpack or []):
        blocked = build_blocked_set(state)
        gun = station_return_steps(role, state, blocked)
        if gun is None:
            gun = MUSTER_BUFFER
        if any(chebyshev(role.pos, Pos(*point)) == 1 for point in extra):
            return remaining > gun + 1
    return remaining > worker_one_wall_rounds(state, role, extra)


def due_wall_gaps(state, role=None):
    """本回合该砌的墙：生存墙优先；齐了且夜前有余量才补其余 16 段。

    施工工、选格、寻路、采石批次都只看这一份列表，避免「锁 16 段」和「只许砌正面」互相卡住。
    """
    core = survival_wall_missing(state)
    if core:
        return core
    if can_extend_walls(state, role):
        return extra_wall_missing(state)
    return []


def defense_wall_missing(state, role=None):
    """兼容旧名：当前该补的墙。"""
    return due_wall_gaps(state, role)


def dusk_must_return(state, role=None):
    """入夜窗口：回炮前只够走到炮位（贴着缺口有石仍可砌 1 格）。"""
    remaining = defense_rounds_remaining(state, role)
    if remaining <= 0:
        return True
    if role is None:
        return remaining <= MUSTER_BUFFER
    blocked = build_blocked_set(state)
    gun = station_return_steps(role, state, blocked)
    if gun is None:
        gun = MUSTER_BUFFER
    return remaining <= gun + 1


def full_wall_build_window(state, role=None):
    """侧翼/后沿：回炮前至少能建完一段才开工。生存墙不走这扇门。"""
    return can_extend_walls(state, role)


def worker_should_build_walls(state, role=None):
    """有 due 缺口就砌；入夜窗口只砌贴身那一格；夜间只补正面关键缺口。"""
    from .tactics import night_wave_cleared, night_near_work_allowed
    if night_wave_cleared(state):
        return True
    if (state.round_no or 0) < 70:
        return True
    remaining = defense_rounds_remaining(state, role)
    if remaining <= 0:
        return bool(night_near_work_allowed(state) and critical_wall_missing(state))
    due = due_wall_gaps(state, role)
    if not due:
        return False
    if dusk_must_return(state, role):
        return bool(role is not None and 'stone' in (role.backpack or [])
                    and any(chebyshev(role.pos, Pos(*p)) == 1 for p in due))
    return True


def stones_cover_wall_plan(state):
    missing = staged_wall_missing(state)
    if not missing:
        return False
    have = sum(r.backpack.count('stone') for r in state.team_our.roles
               if r.role_type == 'worker' and r.health > 0)
    return have >= len(missing)


def staged_walls_incomplete(state):
    return bool(staged_wall_missing(state))


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
    """两人三炮：人少炮多时一人守双火箭、另一人开其余炮；人够时一人一炮。把队友当障碍，里侧开里炮、外侧开外炮。"""
    fighters = sorted((r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0 and r.id not in excluded_ids), key=lambda r: r.id)
    weapons = sorted((r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket') and r.health > 0), key=lambda r: r.id)
    if not fighters or not weapons:
        return {}
    # 被排除的人（做任务/夜间外出）会离开，他们此刻站的格子不算障碍。
    static = build_blocked_set(state) - {
        (r.pos.x, r.pos.y) for r in state.team_our.roles if r.id in excluded_ids
    }
    distances = {}
    for fighter in fighters:
        others = {(r.pos.x, r.pos.y) for r in fighters if r.id != fighter.id}
        blocked = (static | others) - {(fighter.pos.x, fighter.pos.y)}
        for weapon in weapons:
            path = adjacent_path(fighter, weapon.pos, blocked, state)
            distances[fighter.id, weapon.id] = len(path) if path is not None else 10000
    fighter_order = {f.id: i for i, f in enumerate(sorted(fighters, key=lambda r: _fighter_layer(state, r)))}
    weapon_order = {w.id: i for i, w in enumerate(sorted(weapons, key=lambda r: _weapon_layer(state, r)))}
    # 人少炮多时，一人站两门火箭共同邻格可轮流开火：优先让另一人去开非火箭炮，覆盖全部武器。
    dual_pairs = []
    if len(fighters) < len(weapons):
        rockets = [w for w in weapons if w.role_type == 'rocket']
        dual_pairs = [(a, b) for a, b in combinations(rockets, 2)
                      if dual_rocket_stands(state, a, b, static)]
    from .brain import is_day_round
    from .opening_schedule import opening_worker_mode
    night_two_on_three = (not is_day_round(state.round_no)) and bool(dual_pairs)
    builder = next((f for f in fighters if f.role_type == 'worker'
                    and opening_worker_mode(state, f) == 'builder'), None)

    def covered(pairs):
        assigned = {w.id for _, w in pairs}
        extra = set()
        for a, b in dual_pairs:
            if a.id in assigned and b.id not in assigned:
                extra.add(b.id)
            elif b.id in assigned and a.id not in assigned:
                extra.add(a.id)
        return len(assigned | extra)

    def builder_off_rockets(pairs):
        if not night_two_on_three or builder is None:
            return 0
        weapon = next((w for f, w in pairs if f.id == builder.id), None)
        if weapon is None or weapon.role_type != 'rocket':
            return 1
        return 0

    def score_of(pairs):
        base = _assignment_score(pairs, distances, fighter_order, weapon_order)
        return (base[0], builder_off_rockets(pairs), -covered(pairs)) + base[1:]

    best = None
    assignment = {}
    count = min(len(fighters), len(weapons))
    for chosen in permutations(fighters, count):
        for targets in permutations(weapons, count):
            pairs = list(zip(chosen, targets))
            score = score_of(pairs)
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
        if builder_off_rockets(prev_pairs):
            prev_pairs = []
        if prev_pairs:
            prev_score = score_of(prev_pairs)
            if best is None or prev_score <= best:
                assignment = prev
    if persist:
        state.policy_memory['weapon_assignment'] = {str(fid): weapon.id for fid, weapon in assignment.items()}
    return assignment


def builder_dual_rocket(state, role, extra_excluded=()):
    """施工工夜里守双火箭：按经济工外出后的两人三炮分配，拿到他该开的那门火箭。"""
    if role is None or role.role_type != 'worker':
        return None
    from .opening_schedule import opening_worker_mode, opening_worker_roles
    if opening_worker_mode(state, role) != 'builder':
        return None
    eco = opening_worker_roles(state).get('economist')
    excluded = set(extra_excluded)
    if eco:
        excluded.add(eco)
    return assign_weapons(state, excluded_ids=excluded).get(role.id)


def builder_move_to_dual_rockets(role, state, blocked, reserved, reason):
    weapon = builder_dual_rocket(state, role)
    if weapon is None:
        return None
    path = weapon_approach_path(role, weapon, blocked, reserved, state)
    return move_on_path(state, role, path, reserved, reason)


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


def dual_rocket_stands(state, first, second, blocked):
    """两门火箭的共同操控邻格。"""
    if not first or not second or first.role_type != 'rocket' or second.role_type != 'rocket':
        return set()
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    ring = set(wall_ring(state, base)) if base else set()
    obstacles = set(blocked) - {
        (r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')
    }
    first_goals = {
        (p.x, p.y) for p in neighbors8(first.pos, state.map_info.width, state.map_info.height)
        if (p.x, p.y) not in ring and (p.x, p.y) not in obstacles
    }
    second_goals = {
        (p.x, p.y) for p in neighbors8(second.pos, state.map_info.width, state.map_info.height)
        if (p.x, p.y) not in ring and (p.x, p.y) not in obstacles
    }
    return first_goals & second_goals


def dual_rocket_partner(state, weapon, blocked):
    if not weapon or weapon.role_type != 'rocket':
        return None, set()
    partners = []
    for other in state.team_our.roles:
        if other.id == weapon.id or other.role_type != 'rocket' or other.health <= 0:
            continue
        stands = dual_rocket_stands(state, weapon, other, blocked)
        if stands:
            partners.append((chebyshev(weapon.pos, other.pos), other.id, other, stands))
    if not partners:
        return None, set()
    _dist, _id, partner, stands = min(partners)
    return partner, stands


def dual_rocket_path(role, weapon, blocked, state):
    partner, stands = dual_rocket_partner(state, weapon, blocked)
    if not partner or not stands:
        return None
    return path_to_any(role.pos, stands, blocked, state.map_info.width, state.map_info.height)


def other_rocket(state, weapon):
    if not weapon or weapon.role_type != 'rocket':
        return None
    others = [r for r in state.team_our.roles
              if r.role_type == 'rocket' and r.health > 0 and r.id != weapon.id]
    if not others:
        return None
    return min(others, key=lambda r: (chebyshev(weapon.pos, r.pos), r.id))


def control_stand_path(role, weapon, blocked, reserved, state):
    """走到两门火箭共用操控位，以便冷却时切炮；没有共用格就走到另一门旁边。"""
    partner, stands = dual_rocket_partner(state, weapon, blocked)
    if partner is None:
        partner = other_rocket(state, weapon)
    here = (role.pos.x, role.pos.y)
    walkable = mobile_walkable(state, blocked, reserved)
    if stands:
        if here in stands:
            return []
        path = path_to_any(role.pos, stands, walkable, state.map_info.width, state.map_info.height)
        if path is not None:
            return path
    if partner is None:
        return None
    if chebyshev(role.pos, partner.pos) <= 1 and role.pos != partner.pos:
        return []
    return adjacent_path(role, partner.pos, walkable, state)


# 外出的人带着这些东西回家才有用：升级券、修墙道具、战斗道具。空手回来改变不了火箭冷却。
HOME_DEFENSE_ITEMS = frozenset({
    'WeaponUpgradeVoucher1', 'WeaponUpgradeVoucher2',
    'WallUpgradeVoucher1', 'WallUpgradeVoucher2',
    'StationUpgradeVoucher1', 'StationUpgradeVoucher2',
    'WallFixer', 'Bomb', 'DizzyWeapon',
})


def carries_home_defense_item(role):
    return any(item in HOME_DEFENSE_ITEMS for item in (role.backpack or []))


def guns_covered_without(state, excluded_ids, blocked, max_travel=None, enemy_timing=True):
    """两人三炮规则：排除指定角色后，剩下的人（一人站双火箭共同邻格轮流开火）能否覆盖全部武器。
    enemy_timing=False 时只看结构上能不能覆盖（都有路、三门都有人），不因敌人逼近判成“守不住”。"""
    weapons = {r.id for r in state.team_our.roles
               if r.role_type in ('gatling', 'railgun', 'rocket') and r.health > 0}
    if not weapons:
        return False
    assignment = assign_weapons(state, excluded_ids=excluded_ids)
    from .tactics import threat_robots
    robots = threat_robots(state)
    fighters = {r.id: r for r in state.team_our.roles}
    # 被排除的人会离开，他此刻站的格子不算障碍。
    blocked = set(blocked) - {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.id in excluded_ids}
    for fid, weapon in assignment.items():
        fighter = fighters.get(fid)
        path = weapon_approach_path(fighter, weapon, blocked, set(), state) if fighter else None
        if path is None:
            return False
        if not enemy_timing:
            continue
        if max_travel is not None:
            # 有压力时大家都在家：守炮的人必须已经在炮位旁（最多再走 max_travel 步）。
            if len(path) > max_travel:
                return False
            continue
        # 敌人已在路上时，接替的人必须先于敌人到炮位；已经站在炮位旁的人不用赶路，不做这项检查
        # （否则开打后敌人一进 3 格，外出采矿的人就会被一直叫回，变成三人守家）。
        if robots and path and len(path) + MUSTER_BUFFER >= min(
                chebyshev(robot.pos, weapon.pos) for robot in robots):
            return False
    covered = {w.id for w in assignment.values()}
    for weapon in assignment.values():
        partner, _stands = dual_rocket_partner(state, weapon, blocked)
        if partner is not None:
            covered.add(partner.id)
    return weapons <= covered


def weapon_approach_path(role, weapon, blocked, reserved, state):
    """去开炮：已在炮旁就地开火；墙外先走后方开口进院，再绕开队友。"""
    own = {(role.pos.x, role.pos.y)}
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    obstacles = ((blocked | reserved | failed_move_cells(state, role)) - own)
    at_gun = bool(weapon and chebyshev(role.pos, weapon.pos) <= 1 and role.pos != weapon.pos)
    if base is not None and not in_courtyard(state, base, role.pos) and not at_gun:
        into = step_into_courtyard(role, obstacles, state)
        if into:
            return into
        yard = courtyard_cells(state, base)
        enter = path_to_any(role.pos, yard, obstacles, state.map_info.width, state.map_info.height)
        ring = set(wall_ring(state, base)) - {(role.pos.x, role.pos.y)}
        enter_off_ring = path_to_any(role.pos, yard, obstacles | ring, state.map_info.width, state.map_info.height)
        if attack_side_of_front(state, base, role.pos):
            retreat = rear_retreat_path(role, obstacles, state)
            if retreat and (enter is None or len(enter) > 2):
                return retreat
            if enter is not None and len(enter) <= 2:
                return enter
        if enter_off_ring is not None and (
                enter is None or len(enter_off_ring) <= len(enter) + 2
                or (enter and (enter[0].x, enter[0].y) in ring)):
            return enter_off_ring
        if enter:
            return enter
        retreat = rear_retreat_path(role, obstacles, state)
        if retreat:
            return retreat
    if at_gun and weapon and weapon.role_type == 'rocket':
        dual = control_stand_path(role, weapon, obstacles, reserved, state)
        if dual:
            return dual
        return []
    if at_gun:
        return []
    if weapon and weapon.role_type == 'rocket':
        dual = control_stand_path(role, weapon, obstacles, reserved, state)
        if dual is not None:
            return dual
    strict = adjacent_path(role, weapon.pos, obstacles, state)
    if strict is not None:
        return strict
    return adjacent_path(role, weapon.pos, mobile_walkable(state, blocked, reserved), state)


def weapon_candidates(state, base, name, extra_names=(), extra_positions=()):
    """按编制空位补齐：火箭侧两门、另一侧电磁；类型和位置绑定。"""
    occupied = {(r.pos.x, r.pos.y) for r in state.team_our.roles
                if r.health > 0 and r.role_type in ('gatling', 'railgun', 'rocket', 'wall', 'station')}
    occupied.update((p[0], p[1]) for p in extra_positions)
    occupied.update((base.pos.x + dx, base.pos.y - dy) for dx in (0, 1) for dy in (0, 1))
    slots = [p for slot_name, p in weapon_slot_plan(state, base)
             if slot_name == name and p not in occupied]
    if slots:
        return slots
    left, right, bottom, top = defense_bounds(state, base)
    fallback = [(x, y) for x in range(left + 1, right) for y in range(bottom + 1, top)
                if (x, y) not in occupied]
    return fallback


def pioneer_holding_shop_for_voucher(role, state):
    """第一门尚未升级且开拓者已在武器商店旁、资金已闭环时，不要改派回炮空转。"""
    from .brain import find_zone, is_day_round, item_cost
    if role.role_type != 'pioneer' or role.health <= 0:
        return False
    if not is_day_round(state.round_no) or day_rounds_remaining(state.round_no) <= 0:
        return False
    if survival_walls_locked(state) and 'WeaponUpgradeVoucher1' not in (role.backpack or []):
        return False
    if live_l2_weapon_count(state) >= REQUIRED_OPENING_UPGRADES:
        return False
    if state.phase_task:
        return False
    funding = opening_upgrade_funding(state)
    if not funding.get('funded') and not funding.get('pending'):
        return False
    if (state.team_our.gold_num if state.team_our else 0) >= item_cost('WeaponUpgradeVoucher1', state):
        shop = find_zone(state, 'weaponShop')
        return bool(shop and chebyshev(role.pos, shop.pos) <= 1)
    shop = find_zone(state, 'weaponShop')
    return bool(funding.get('pending') and shop and chebyshev(role.pos, shop.pos) <= 1)


def pioneer_wait_weapon_shop(role, state, blocked, reserved):
    """第一门资金已闭环、又没有可行任务时，去武器商店买券；未闭环不去空等。"""
    from .brain import find_zone, is_day_round, item_cost
    from .pioneer_schedule import has_task_reservation
    if role.role_type != 'pioneer' or role.health <= 0:
        return None
    if not is_day_round(state.round_no) or day_rounds_remaining(state.round_no) <= 0:
        return None
    if survival_walls_locked(state) and 'WeaponUpgradeVoucher1' not in (role.backpack or []):
        if (state.team_our.gold_num if state.team_our else 0) < item_cost('WeaponUpgradeVoucher1', state):
            return None
    if live_l2_weapon_count(state) >= REQUIRED_OPENING_UPGRADES:
        return None
    if state.phase_task or has_task_reservation(state, role):
        return None
    funding = opening_upgrade_funding(state)
    gold = state.team_our.gold_num if state.team_our else 0
    if gold < item_cost('WeaponUpgradeVoucher1', state) and not funding.get('pending') and not funding.get('funded'):
        return None
    if gold < item_cost('WeaponUpgradeVoucher1', state) and not funding.get('pending'):
        return None
    shop = find_zone(state, 'weaponShop')
    if shop is None:
        return None
    path = adjacent_path(role, shop.pos, blocked | reserved, state)
    if path is None:
        return None
    if not path:
        trace(state, role.id, 'pioneer_wait_shop', '开拓者已在武器商店旁，等待买券或金币到账')
        return None
    return move_on_path(state, role, path, reserved, '升级资金已闭环，开拓者去武器商店买券')


def pioneer_stay_clear(role, state, blocked, reserved, assignments=None):
    """开拓者不能采集或建造；让开墙线和炮位，无任务时去已分配武器。"""
    from .brain import own_station
    if role.role_type != 'pioneer' or role.health <= 0:
        return None
    base = own_station(state)
    if base is None:
        trace(state, role.id, 'pioneer_stay_clear_no_station', '找不到基地，开拓者本回合无命令')
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
    trace(state, role.id, 'pioneer_stay_clear_no_weapon', '没有分配到武器，开拓者本回合无命令',
          assignments_count=len(assignments))
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
    wait = pioneer_wait_weapon_shop(role, state, blocked, reserved)
    if wait:
        return wait
    if pioneer_holding_shop_for_voucher(role, state):
        return None
    return pioneer_stay_clear(role, state, blocked, reserved, assignments)


STONE_BUILD_ROUNDS = 2  # 每块石头回家后大约要 移动+建造 两回合。


def keep_collecting_stone(role, state, blocked, reserved, base):
    """人在石矿边、石头还不够这一趟批量时继续采。凑够 STONE_BATCH 或缺口数就回家建。
    背包满、离天黑不够“回家 + 建完手上石头”、或矿已采空时才停。"""
    from .brain import is_day_round
    if not is_day_round(state.round_no) or not state.map_info:
        return None
    stones = role.backpack.count('stone')
    if stones <= 0:
        return None  # 没石头交给原流程去找矿
    cap = role.back_pack_capability or 0
    if cap and len(role.backpack) >= cap:
        return None
    need = len(due_wall_gaps(state, role))
    if need <= 0 or stones >= min(need, STONE_BATCH):
        return None
    mines = [z for z in state.map_info.zones
             if z.neutral_type == 'stone' and chebyshev(role.pos, z.pos) <= 1]
    if not mines:
        return None  # 已经离开矿点：先把手上的石头建完，剩下的慢慢补
    home = chebyshev(role.pos, base.pos)
    if day_rounds_remaining(state.round_no) <= 1 + home + STONE_BUILD_ROUNDS * (stones + 1) + MUSTER_BUFFER:
        return None
    mine = min(mines, key=lambda z: (z.pos.x, z.pos.y))
    trace(state, role.id, 'stone_batch_collect', '石头还不够这一趟批量，继续在矿点采集',
          stones=stones, need=need, batch=STONE_BATCH)
    return selected(state, role.id, {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]},
                    '攒够一批石头再回家修墙')


def replenish_walls(role, state, blocked, reserved, primary_only=False, allow_build=True):
    """缺墙就是持续施工任务，缺石主动找石矿，不转去采铜铁。仅工人白天：开拓者不能 collect/build。"""
    from .brain import is_day_round, own_station, try_build
    if role.role_type != 'worker' or not is_day_round(state.round_no):
        return False, None
    base = own_station(state)
    if base is None:
        return False, None
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    if primary_only:
        missing = set(critical_wall_missing(state))
    else:
        missing = set(due_wall_gaps(state, role))
    if not missing:
        return False, None
    trace(state, role.id, 'persistent_wall_plan', '按当前该砌的墙补，缺石就采石', missing=sorted(missing),
          wall_goal=len(missing | existing))
    batch = keep_collecting_stone(role, state, blocked, reserved, base)
    if batch:
        return True, batch
    if 'stone' in role.backpack:
        from .economy import clear_mine_target
        clear_mine_target(state, role.id)
        if not allow_build:
            trace(state, role.id, 'stones_reserved_for_late_day',
                  '回炮前不够再建一段侧翼，施工工去双火箭位待命', stones=role.backpack.count('stone'), missing=len(missing))
            cmd = builder_move_to_dual_rockets(role, state, blocked, reserved,
                                               '入夜前不够再建一段，先去双火箭位')
            if cmd:
                return True, cmd
            cmd = builder_unjam_walls(role, state, blocked, reserved, allow_mine=False)
            return True, cmd
        cmd = try_build(role, state, blocked, reserved)
        if cmd:
            state.policy_memory['wall_work_attempted'] = True
            return True, cmd
        cmd = builder_unjam_walls(role, state, blocked, reserved, allow_mine=False)
        if cmd:
            return True, cmd
        # 白天砌不上就停在缺口旁等下一回合，不要改去双火箭位来回跑。
        trace(state, role.id, 'builder_hold_at_gap',
              '手里有石但本回合砌不上，留在施工任务上不改去炮位',
              stones=role.backpack.count('stone'), missing=len(missing))
        return True, None
    cmd = builder_unjam_walls(role, state, blocked, reserved, allow_mine=True)
    if cmd:
        return True, cmd
    trace(state, role.id, 'wall_material_blocked', '缺墙但石矿不可达或背包已满', backpack_count=len(role.backpack))
    return True, None


def _buildable_wall_paths(role, state, blocked, reserved):
    """只走向能砌的缺口：跳过失败冷却和 safe_wall 拒建的格子，避免对着砌不上的墙空转。"""
    from .brain import own_station
    base = own_station(state)
    if base is None:
        return []
    existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
    assignments = assign_weapons(state)
    ranked = []
    for point in due_wall_gaps(state, role):
        if point in existing or point in blocked | reserved:
            continue
        if (point[0], point[1], 'wall') in state.failed_build_spots:
            continue
        if not safe_wall(state, point, blocked | reserved, assignments):
            continue
        path = wall_approach_path(role, Pos(*point), blocked | reserved, state)
        if path is None:
            continue
        ranked.append((len(path), point, path))
    ranked.sort()
    return ranked


def step_toward_wall_gap(role, point, blocked, reserved, state):
    """BFS 接近失败时，朝缺口走一步，避免有石空转。"""
    if not state.map_info:
        return None
    target = Pos(*point)
    if chebyshev(role.pos, target) <= 1:
        return None
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    yard = courtyard_cells(state, base) if base else set()
    here_attack = bool(base and attack_side_of_front(state, base, role.pos))
    best = None
    for step in neighbors8(role.pos, state.map_info.width, state.map_info.height):
        key = (step.x, step.y)
        if key in blocked | reserved:
            continue
        if base and not here_attack and attack_side_of_front(state, base, step):
            continue
        closer = chebyshev(step, target)
        if closer >= chebyshev(role.pos, target):
            continue
        rank = (closer, 0 if key in yard else 1, 1 if base and attack_side_of_front(state, base, step) else 0, key)
        if best is None or rank < best[0]:
            best = (rank, step)
    if best is None:
        return None
    return [best[1]]


def builder_unjam_walls(role, state, blocked, reserved, allow_mine=True):
    """施工工缺墙却没发出建造时：丢铜铁、贴院迈进、走向可砌缺口或去采石。"""
    from .brain import own_station
    from .economy import go_mine, clear_mine_target
    base = own_station(state)
    if base is None:
        return None
    drop = drop_nonstone_for_walls(role, state)
    if drop:
        return drop
    if "stone" in role.backpack:
        clear_mine_target(state, role.id)
        ranked = _buildable_wall_paths(role, state, blocked, reserved)
        if not ranked:
            existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
            for point in due_wall_gaps(state, role):
                if point in existing or point in blocked | reserved:
                    continue
                if (point[0], point[1], 'wall') in state.failed_build_spots:
                    continue
                path = wall_approach_path(role, Pos(*point), blocked | reserved, state)
                if path is None:
                    continue
                ranked.append((len(path), point, path))
            ranked.sort()
        if ranked:
            _cost, point, path = ranked[0]
            if not path and chebyshev(role.pos, Pos(*point)) == 1:
                reserved.add(point)
                return selected(state, role.id, {'action': 'build', 'name': 'wall', 'targetPos': [{'x': point[0], 'y': point[1]}]},
                                '贴着可砌缺口，就地建造避免空转')
            cmd = move_on_path(state, role, path, reserved, '走向最近可砌墙缺口，避免站着空转')
            if cmd:
                return cmd
        if not in_courtyard(state, base, role.pos):
            into = step_into_courtyard(role, blocked | reserved, state)
            if into:
                return move_on_path(state, role, into, reserved, '贴着院子先迈进去再施工')
            path = interior_retreat_path(role, blocked | reserved, state)
            if path:
                return move_on_path(state, role, path, reserved, '墙外空转，先回院子再施工')
        existing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
        greedy_best = None
        for point in due_wall_gaps(state, role):
            if point in existing or point in blocked | reserved:
                continue
            if chebyshev(role.pos, Pos(*point)) == 1:
                reserved.add(point)
                return selected(state, role.id, {'action': 'build', 'name': 'wall', 'targetPos': [{'x': point[0], 'y': point[1]}]},
                                '贴着缺口且寻路失败，就地建造避免空转')
            greedy = step_toward_wall_gap(role, point, blocked, reserved, state)
            if not greedy:
                continue
            rank = (chebyshev(role.pos, Pos(*point)), point)
            if greedy_best is None or rank < greedy_best[0]:
                greedy_best = (rank, greedy)
        if greedy_best:
            cmd = move_on_path(state, role, greedy_best[1], reserved, 'BFS接近失败，朝缺口迈一步避免空转')
            if cmd:
                return cmd
        return None
    if not in_courtyard(state, base, role.pos):
        # 没石头时先去采石，不要空手走回家再出门。
        if allow_mine and len(role.backpack) < (role.back_pack_capability or 1):
            mined = go_mine(
                role, state, blocked, reserved, want_ores=("stone",), purpose="stone",
                travel_reason="防线尚未完成，专程采石",
                collect_reason="采集下一段城墙所需石料",
            )
            if mined:
                return mined
        into = step_into_courtyard(role, blocked | reserved, state)
        if into:
            return move_on_path(state, role, into, reserved, '贴着院子先迈进去再施工')
        path = interior_retreat_path(role, blocked | reserved, state)
        if path:
            return move_on_path(state, role, path, reserved, '墙外空转，先回院子再施工')
    if not allow_mine or len(role.backpack) >= (role.back_pack_capability or 0):
        return None
    return go_mine(
        role, state, blocked, reserved, want_ores=("stone",), purpose="stone",
        travel_reason="防线尚未完成，专程采石",
        collect_reason="采集下一段城墙所需石料",
    )


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
    from .brain import is_day_round
    if role.role_type != 'worker' or 'stone' not in role.backpack:
        return None
    if not is_day_round(state.round_no):
        return None  # 夜里不能建造（任务书4.4），走过去也封不上，别占用夜间采矿的人
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
            if attack_side_of_front(state, base, role.pos):
                # 人此刻在迎敌侧，不因此拒建；进出改走后方开口。
                continue
            if path_to_any(role.pos, yard, obstacles, state.map_info.width, state.map_info.height) is None:
                return False
        # 首日墙还没备够石头时，不能先把院内到石矿/商店的出口彻底封死。
        # 否则建造工会修到半圈后被困在院内，后续白天只剩 no_reachable_work。
        if day_index(state) == 0:
            workers = [r for r in actors if r.role_type == 'worker' and r.health > 0]
            stone_zones = [z.pos for z in state.map_info.zones if z.neutral_type == 'stone']
            if workers and stone_zones:
                try:
                    missing_after = len(staged_wall_missing(state)) - (1 if point in staged_wall_missing(state) else 0)
                except RecursionError:
                    missing_after = 0
                carried_stone = sum(r.backpack.count('stone') for r in workers)
                if carried_stone < max(0, missing_after):
                    anchor = courtyard_anchor(state, base, obstacles)
                    before = obstacles - {point}
                    had_stone_path = any(
                        adjacent_path(_actor_at(workers[0], anchor), mine, before, state) is not None
                        for mine in stone_zones
                    )
                    keeps_stone_path = any(
                        adjacent_path(_actor_at(workers[0], anchor), mine, obstacles, state) is not None
                        for mine in stone_zones
                    )
                    if had_stone_path and not keeps_stone_path:
                        return False
    # 同时保留原本可达的经济/任务目的地，不能只保证能回炮台。
    before = obstacles - {point}
    destinations = [z.pos for z in state.map_info.zones if z.neutral_type in ('vendor', 'weaponShop', 'stone')]
    for role in actors:
        actor = stationed(role)
        targets = destinations if role.role_type == 'worker' else [t.task_position for t in state.team_our.player_tasks if t.is_valid]
        for target in targets:
            if adjacent_path(actor, target, before, state) is not None and adjacent_path(actor, target, obstacles, state) is None:
                return False
    return True


def drop_nonstone_for_walls(role, state):
    cap = role.back_pack_capability or 0
    if cap and len(role.backpack or []) < cap:
        return None
    for name in ('copper', 'iron'):
        if name in (role.backpack or []):
            return selected(state, role.id, {'action': 'drop', 'name': name},
                            '生存墙需要石头，丢弃铜铁腾出背包')
    return None


def release_unbought_opening_weapon_jobs(state, reason):
    for role_id, job in list(state.worker_item_jobs.items()):
        if job.get('kind') != 'weapon':
            continue
        owner = next((r for r in (state.team_our.roles if state.team_our else [])
                      if r.id == role_id and r.health > 0), None)
        if owner is not None and job.get('item') in owner.backpack:
            continue
        del state.worker_item_jobs[role_id]
        (state.policy_memory.get('opening_job_progress') or {}).pop(str(role_id), None)
        trace(state, role_id, 'weapon_job_released', '未买到手的升级券任务已释放，工人改去生存墙',
              reason=reason)


def update_opening_job_progress(state, role):
    job = state.worker_item_jobs.get(role.id)
    mem = state.policy_memory.setdefault('opening_job_progress', {})
    key = str(role.id)
    snap = {
        'round': state.round_no,
        'pos': (role.pos.x, role.pos.y),
        'gold': state.team_our.gold_num if state.team_our else 0,
        'backpack': tuple(role.backpack or []),
        'stage': None if not job else ('apply' if job.get('item') in role.backpack else 'buy'),
        'has_item': bool(job and job.get('item') in role.backpack),
        'job_kind': None if not job else job.get('kind'),
    }
    prev = mem.get(key) or {}
    progressed = (
        prev.get('pos') != snap['pos']
        or prev.get('gold') != snap['gold']
        or prev.get('backpack') != snap['backpack']
        or prev.get('has_item') != snap['has_item']
        or prev.get('stage') != snap['stage']
    )
    if progressed or not prev:
        snap['stalled_rounds'] = 0
        snap['last_progress_round'] = state.round_no
    else:
        snap['stalled_rounds'] = int(prev.get('stalled_rounds') or 0) + 1
        snap['last_progress_round'] = prev.get('last_progress_round')
    mem[key] = snap
    return snap


def release_stalled_opening_jobs(state, survival_mode):
    for role_id, job in list(state.worker_item_jobs.items()):
        if job.get('kind') != 'weapon':
            continue
        role = next((r for r in (state.team_our.roles if state.team_our else [])
                     if r.id == role_id), None)
        if role is None or role.health <= 0:
            del state.worker_item_jobs[role_id]
            continue
        if job.get('item') in role.backpack:
            continue
        gold = state.team_our.gold_num if state.team_our else 0
        from .brain import find_zone, item_cost
        cost = item_cost(job.get('item') or 'WeaponUpgradeVoucher1', state)
        if survival_mode and gold < cost:
            del state.worker_item_jobs[role_id]
            trace(state, role.id, 'weapon_job_released', '生存墙模式释放未购入的升级券任务',
                  reason='survival_walls')
            continue
        if gold < cost:
            del state.worker_item_jobs[role_id]
            trace(state, role.id, 'weapon_job_released', '金币不足，释放无法推进的买券任务',
                  reason='gold_short')
            continue
        shop = find_zone(state, 'weaponShop')
        if shop is not None:
            from .grid import build_blocked_set
            blocked = build_blocked_set(state)
            if adjacent_path(role, shop.pos, blocked, state) is None:
                del state.worker_item_jobs[role_id]
                trace(state, role.id, 'weapon_job_released', '武器商店不可达，释放买券任务',
                      reason='shop_unreachable')
                continue
        snap = update_opening_job_progress(state, role)
        if int(snap.get('stalled_rounds') or 0) >= JOB_STALL_ROUNDS:
            del state.worker_item_jobs[role_id]
            trace(state, role.id, 'weapon_job_stalled', '升级券任务连续无进展，重新规划',
                  stalled_rounds=snap['stalled_rounds'], job_stage=snap.get('stage'))


def claim_opening_wall(role, state, candidates, blocked, reserved, claimed, assignments):
    """选出本回合真正能接近或建造的墙位；失败不占用 claim。"""
    sticky = tuple(state.policy_memory.get('opening_wall_targets', {}).get(str(role.id), ()))
    if sticky and sticky not in candidates:
        state.policy_memory.get('opening_wall_targets', {}).pop(str(role.id), None)
        sticky = ()

    def wall_sort_key(point):
        path = wall_approach_path(role, Pos(*point), blocked | reserved, state)
        unreachable = path is None
        from .brain import own_station
        base = own_station(state)
        pri = 0 if base is None else wall_priority(state, base, point)
        adjacent = 0 if path == [] else 1
        return (unreachable, pri, adjacent, 0 if path is None else len(path),
                0 if point == sticky else 1, point)

    for point in sorted(candidates, key=wall_sort_key):
        occupied = (blocked | reserved | claimed) - {(role.pos.x, role.pos.y)}
        if point in occupied or (*point, 'wall') in state.failed_build_spots:
            continue
        if not safe_wall(state, point, blocked | claimed, assignments):
            continue
        path = wall_approach_path(role, Pos(*point), blocked | reserved, state)
        if path is None:
            continue
        claimed.add(point)
        state.policy_memory.setdefault('opening_wall_targets', {})[str(role.id)] = list(point)
        if path:
            cmd = move_on_path(state, role, path, reserved, '从院内接近迎敌墙缺口')
            if not cmd:
                claimed.discard(point)
                state.policy_memory.get('opening_wall_targets', {}).pop(str(role.id), None)
                continue
            return cmd
        cmd = selected(state, role.id, {
            'action': 'build', 'name': 'wall', 'targetPos': [{'x': point[0], 'y': point[1]}],
        }, '建造迎敌防线')
        reserved.add(point)
        blocked.add(point)
        state.policy_memory.get('opening_wall_targets', {}).pop(str(role.id), None)
        state.policy_memory['wall_work_attempted'] = True
        return cmd
    return None


def opening_yard_wait(role, state, blocked, reserved):
    path = interior_retreat_path(role, (blocked | reserved) - {(role.pos.x, role.pos.y)}, state)
    if path == []:
        trace(state, role.id, 'survival_wait_in_yard', '已在院内等待下一处可建缺口')
        return None
    return move_on_path(state, role, path, reserved, '移动到院内施工等待点')


def opening_worker_survival_action(role, state, blocked, reserved, claimed, assignments, missing):
    """生存墙模式下给工人明确工作：有石修墙，无石采石，铜铁占包则清包。"""
    from .economy import go_mine, liquidate, worker_has_metal
    stones = role.backpack.count('stone')
    cap = role.back_pack_capability or 0
    full = cap and len(role.backpack) >= cap
    batch_target = min(STONE_BATCH, cap or STONE_BATCH, len(missing)) if missing else 0
    urgent_ready = stones > 0 and day_rounds_remaining(state.round_no) <= MUSTER_BUFFER + 6
    batch_ready = stones >= batch_target if batch_target else stones > 0
    if stones > 0 and missing and (full or batch_ready or urgent_ready):
        cmd = claim_opening_wall(role, state, missing, blocked, reserved, claimed, assignments)
        if cmd:
            return cmd, 'BUILD_SURVIVAL_WALL'
    if worker_has_metal(role, state) and stones == 0 and full:
        if any(z.neutral_type == 'vendor' for z in (state.map_info.zones if state.map_info else [])):
            handled, cmd = liquidate(role, state, blocked, reserved)
            if cmd:
                return cmd, 'CASHOUT'
            if handled:
                dropped = drop_nonstone_for_walls(role, state)
                if dropped:
                    trace(state, role.id, 'blocked_by_nonstone_inventory',
                          'vendor 不可达或无法出售，丢弃铜铁以便采石')
                    return dropped, 'BLOCKED'
                trace(state, role.id, 'blocked_by_nonstone_inventory',
                      '背包铜铁挡住采石，且当前无法变现')
    if (not cap or len(role.backpack) < cap) and missing:
        cmd = go_mine(
            role, state, blocked, reserved, want_ores=('stone',), purpose='stone',
            travel_reason='生存墙缺石，前往可达石矿',
            collect_reason='采集最低防线所需石料',
        )
        if cmd:
            return cmd, 'MINE_STONE'
        trace(state, role.id, 'stone_mine_unreachable', '石矿不可达，改去院内等待',
              path_status='unreachable')
        wait = opening_yard_wait(role, state, blocked, reserved)
        return wait, 'BLOCKED'
    if stones > 0:
        wait = opening_yard_wait(role, state, blocked, reserved)
        return wait, 'BUILD_SURVIVAL_WALL'
    wait = opening_yard_wait(role, state, blocked, reserved)
    return wait, 'BLOCKED'


def log_worker_no_command(state, role, worker_state, reason, budget, job_owner=None, target=None,
                          path_status=None, reserved_conflict=False):
    trace(state, role.id, 'worker_no_command', '工人本回合没有可执行指令',
          worker_state=worker_state, no_command_reason=reason, job_owner=job_owner,
          backpack=list(role.backpack or []), gold=state.team_our.gold_num if state.team_our else 0,
          position={'x': role.pos.x, 'y': role.pos.y}, target=target, path_status=path_status,
          allow_sell=budget.get('allow_sell'), allow_income_mine=budget.get('allow_income_mine'),
          allow_stone_mine=budget.get('allow_stone_mine'), allow_walls=budget.get('allow_walls'),
          reserved_conflict=reserved_conflict)


def opening_hold_weapon_ok(state, survival_missing, remaining, muster_need):
    from .tactics import imminent_contact
    if remaining <= muster_need:
        return True
    if imminent_contact(state):
        return True
    if not survival_missing:
        return True
    return False


def opening_live_weapon(role, assignments):
    weapon = assignments.get(role.id)
    if weapon is None or weapon.health <= 0:
        return None
    return weapon


def opening_worker_ensure_work(role, state, blocked, reserved, claimed, assignments, missing,
                               survival_missing, budget, remaining, muster_need, allow_sell):
    """无命令时的显式兜底：必须生成指令，或证明已在合法等待点。"""
    from .economy import go_mine, liquidate, worker_has_metal
    from .tactics import imminent_contact
    hold_ok = opening_hold_weapon_ok(state, survival_missing, remaining, muster_need)
    if hold_ok or imminent_contact(state):
        weapon = opening_live_weapon(role, assignments)
        if weapon:
            path = weapon_approach_path(role, weapon, blocked, reserved, state)
            cmd = move_on_path(state, role, path, reserved, '回炮/守炮')
            if cmd:
                return cmd, 'MUSTER' if remaining <= muster_need else 'HOLD_WEAPON'
            if path == []:
                return None, 'HOLD_WEAPON'
        return opening_yard_wait(role, state, blocked, reserved), 'BLOCKED'
    if survival_missing:
        stones = role.backpack.count('stone')
        cap = role.back_pack_capability or 0
        full = cap and len(role.backpack) >= cap
        batch_target = min(STONE_BATCH, cap or STONE_BATCH, len(missing or survival_missing)) if (missing or survival_missing) else 0
        urgent_ready = stones > 0 and remaining <= MUSTER_BUFFER + 6
        batch_ready = stones >= batch_target if batch_target else stones > 0
        if stones > 0 and (full or batch_ready or urgent_ready):
            cmd = claim_opening_wall(role, state, missing or survival_missing, blocked, reserved, claimed, assignments)
            if cmd:
                return cmd, 'BUILD_SURVIVAL_WALL'
        if worker_has_metal(role, state) and (cap and len(role.backpack) >= cap) and stones == 0:
            handled, cmd = liquidate(role, state, blocked, reserved)
            if cmd:
                return cmd, 'CASHOUT'
            dropped = drop_nonstone_for_walls(role, state)
            if dropped:
                return dropped, 'BLOCKED'
        if not cap or len(role.backpack) < cap:
            cmd = go_mine(
                role, state, blocked, reserved, want_ores=('stone',), purpose='stone',
                travel_reason='生存墙未完成，兜底采石',
                collect_reason='采集最低防线所需石料',
            )
            if cmd:
                return cmd, 'MINE_STONE'
        cmd, status = opening_worker_survival_action(
            role, state, blocked, reserved, claimed, assignments, missing or survival_missing)
        return cmd, status
    if allow_sell and worker_has_metal(role, state):
        handled, cmd = liquidate(role, state, blocked, reserved)
        if cmd:
            return cmd, 'CASHOUT'
    job = state.worker_item_jobs.get(role.id)
    if job and job.get('item') in (role.backpack or []):
        from .brain import decide_shop_item_job
        cmd = decide_shop_item_job(role, state, blocked, reserved)
        if cmd:
            return cmd, 'APPLY_VOUCHER'
    weapon = opening_live_weapon(role, assignments)
    if weapon:
        path = weapon_approach_path(role, weapon, blocked, reserved, state)
        cmd = move_on_path(state, role, path, reserved, '最低墙已完成，前往武器岗位')
        if cmd:
            return cmd, 'HOLD_WEAPON'
        if path == []:
            return None, 'HOLD_WEAPON'
    return opening_yard_wait(role, state, blocked, reserved), 'BLOCKED'


def plan_opening(state):
    from .brain import is_day_round, own_station
    from .opening_schedule import plan_opening_fsm
    if not is_day_round(state.round_no):
        cycle = (state.round_no or 0) % 130
        trace(state, None, 'opening_night_guard', '开局计划只在官方白天运行',
              round_no=state.round_no, cycle=cycle, remaining=day_rounds_remaining(state.round_no),
              defense_remaining=defense_rounds_remaining(state))
        return {}
    if own_station(state) is None:
        return {}
    return plan_opening_fsm(state)
