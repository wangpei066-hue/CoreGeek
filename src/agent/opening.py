"""第一天：三座火箭炮 -> 筹资升最前一门 -> 迎敌7-8段墙 -> 夜间三人三炮。

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
DAY1_WALL_TARGET = 8
DAY2_WALL_TARGET = 12
VOUCHER_USE_SLACK = 4  # 买券后走到最前火箭并使用的余量，不是官方耗时。
WALL_STEP_SLACK = 1    # 每段墙在建造外再留1回合走位。
FALLBACK_TRAVEL = 8


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


def wall_ring(state, base):
    """迎敌双层防线：内层完整、外层留口、侧翼补墙，后方开放。"""
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    front = right if direction == 1 else left
    protected_rear = front - 3 * direction
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
    return FALLBACK_TRAVEL if best is None else best


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
    mine_travel = _shortest_adjacent(workers, mines, blocked, state) if collect and mines else 0
    gaps = [Pos(*p) for p in missing]
    gap_travel = _shortest_adjacent(workers, gaps, blocked, state) if gaps else 0
    return mine_travel + -(-collect // hands) + gap_travel + n * WALL_STEP_SLACK + -(-n // hands)


def voucher_trip_rounds(state, blocked, gold, need_sell):
    """买并使用一张武器升级券的路程估计；需要卖矿时计入小贩往返。"""
    from .brain import item_cost
    workers = [r for r in state.team_our.roles if r.role_type == 'worker' and r.health > 0]
    shops = [z.pos for z in state.map_info.zones if z.neutral_type == 'weaponShop']
    shop_travel = _shortest_adjacent(workers, shops, blocked, state) if shops else FALLBACK_TRAVEL
    trip = shop_travel + 1 + VOUCHER_USE_SLACK
    if gold >= item_cost('WeaponUpgradeVoucher1', state) or any(
            'WeaponUpgradeVoucher1' in r.backpack for r in workers):
        return trip
    if not need_sell:
        return trip
    vendors = [z.pos for z in state.map_info.zones if z.neutral_type == 'vendor']
    if not vendors:
        return None
    return _shortest_adjacent(workers, vendors, blocked, state) + 3 + trip


def opening_time_budget(state, missing, remaining, muster_need, gold, upgraded_once, blocked):
    """首日切换点：剩下的回合必须够修完7-8段墙；若再买券就会误工则先修墙。"""
    wall_need = wall_finish_rounds(state, missing, blocked)
    wall_deadline = wall_need + muster_need
    can_finish_walls = remaining > wall_deadline
    has_voucher = any('WeaponUpgradeVoucher1' in r.backpack
                      for r in state.team_our.roles if r.role_type in ('worker', 'pioneer'))
    from .brain import item_cost
    voucher_cost = item_cost('WeaponUpgradeVoucher1', state)
    gold_ready = gold >= voucher_cost or has_voucher
    sell_trip = voucher_trip_rounds(state, blocked, gold, need_sell=not gold_ready)
    if upgraded_once:
        return {
            'wall_need': wall_need, 'wall_deadline': wall_deadline, 'sell_trip': sell_trip,
            'allow_walls': True, 'allow_upgrade': False, 'allow_sell': False, 'allow_mine': False,
            'can_finish_walls': can_finish_walls,
        }
    if remaining <= wall_deadline:
        # 只够修墙和回防，不再卖矿或绕路买券。
        return {
            'wall_need': wall_need, 'wall_deadline': wall_deadline, 'sell_trip': sell_trip,
            'allow_walls': True, 'allow_upgrade': False, 'allow_sell': False, 'allow_mine': False,
            'can_finish_walls': can_finish_walls,
        }
    if gold_ready:
        return {
            'wall_need': wall_need, 'wall_deadline': wall_deadline, 'sell_trip': sell_trip,
            'allow_walls': True, 'allow_upgrade': True, 'allow_sell': False, 'allow_mine': False,
            'can_finish_walls': can_finish_walls,
        }
    if sell_trip is None or remaining <= wall_deadline + sell_trip:
        return {
            'wall_need': wall_need, 'wall_deadline': wall_deadline, 'sell_trip': sell_trip,
            'allow_walls': True, 'allow_upgrade': False, 'allow_sell': False, 'allow_mine': False,
            'can_finish_walls': can_finish_walls,
        }
    return {
        'wall_need': wall_need, 'wall_deadline': wall_deadline, 'sell_trip': sell_trip,
        'allow_walls': False, 'allow_upgrade': True, 'allow_sell': True, 'allow_mine': True,
        'can_finish_walls': can_finish_walls,
    }


def staged_wall_plan(state, base):
    """按天限制迎敌墙数量：首日正面约8段，次日补到12段，之后再铺满一层。"""
    plan = primary_wall_plan(state, base)
    day = day_index(state)
    if day <= 0:
        return plan[:DAY1_WALL_TARGET]
    if day == 1:
        return plan[:DAY2_WALL_TARGET]
    return plan


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


def assign_weapons(state, excluded_ids=()):
    """至多三座武器，枚举一对一分配，优先可达并最小化总路程。"""
    fighters = sorted((r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0 and r.id not in excluded_ids), key=lambda r: r.id)
    weapons = sorted((r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket') and r.health > 0), key=lambda r: r.id)
    if not fighters or not weapons:
        return {}
    blocked = (build_blocked_set(state) - {(r.pos.x, r.pos.y) for r in fighters}) | movement_avoid(state)
    distances = {}
    for fighter in fighters:
        for weapon in weapons:
            path = adjacent_path(fighter, weapon.pos, blocked, state)
            distances[fighter.id, weapon.id] = len(path) if path is not None else 10000
    best = None
    assignment = {}
    count = min(len(fighters), len(weapons))
    for chosen in permutations(fighters, count):
        for targets in permutations(weapons, count):
            pairs = list(zip(chosen, targets))
            score = sum(distances[f.id, w.id] for f, w in pairs)
            if best is None or score < best:
                best = score
                assignment = {f.id: w for f, w in pairs}
    return assignment


def move_on_path(state, role, path, reserved, reason):
    if path:
        step = path[0]
        reserved.add((step.x, step.y))
        return selected(state, role.id, {'action': 'move', 'targetPos': [{'x': step.x, 'y': step.y}]}, reason)
    trace(state, role.id, 'at_destination' if path == [] else 'unreachable',
          '已到达目标位置' if path == [] else '当前目标不可达', task=reason)
    return None


def station_path(role, weapon, blocked, state):
    """操控位置不能停在未来墙体缺口上，避免堵塞回城通道。"""
    base = next((r for r in state.team_our.roles if r.role_type == 'station'), None)
    ring = set(wall_ring(state, base)) if base else set()
    goals = {(p.x, p.y) for p in neighbors8(weapon.pos, state.map_info.width, state.map_info.height)
             if (p.x, p.y) not in ring and ((p.x, p.y) not in blocked or p == role.pos)}
    return path_to_any(role.pos, goals, blocked, state.map_info.width, state.map_info.height)


def weapon_candidates(state, base, name, extra_names=(), extra_positions=()):
    """三座火箭都尽量贴内墙前列，第一座占迎敌中线，其余分列基地上下两侧。

    extra_names 保留与建造规划接口兼容。
    """
    left, right, bottom, top = defense_bounds(state, base)
    direction = attack_direction(state, base)
    front = right if direction == 1 else left
    ideal_x = front - direction * 1
    existing = [(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
    existing.extend((p[0], p[1]) for p in extra_positions)

    def score(point):
        x, y = point
        forward = abs(x - ideal_x)
        flank = abs(y - base.pos.y)
        if not existing:
            return (forward, abs(y - base.pos.y), point)
        mid_y = sum(ey for _, ey in existing) / len(existing)
        return (forward, -abs(y - mid_y), -flank, point)

    ranked = sorted(((x, y) for x in range(left + 1, right) for y in range(bottom + 1, top)), key=score)
    frontish = [p for p in ranked if abs(p[0] - ideal_x) <= 1]

    def crowded(point):
        return sum(max(abs(point[0] - ex), abs(point[1] - ey)) <= 1 for ex, ey in existing)

    openish = [p for p in frontish if crowded(p) < 2]
    return openish or frontish or ranked


def replenish_walls(role, state, blocked, reserved, primary_only=False):
    """缺墙就是持续施工任务，缺石主动找石矿，不转去采铜铁。"""
    from .brain import own_station, try_build
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
        return True, try_build(role, state, blocked, reserved)
    paths = [adjacent_path(role, z.pos, blocked | reserved, state) for z in state.map_info.zones if z.neutral_type == 'stone']
    mines = [z for z in state.map_info.zones if z.neutral_type == 'stone']
    choices = [(p, z) for p, z in zip(paths, mines) if p is not None]
    if choices and len(role.backpack) < role.back_pack_capability:
        path, mine = min(choices, key=lambda pair: len(pair[0]))
        if path:
            return True, move_on_path(state, role, path, reserved, '双层墙尚未完成，专程采石')
        return True, selected(state, role.id, {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]}, '采集下一段城墙所需石料')
    trace(state, role.id, 'wall_material_blocked', '缺墙但石矿不可达或背包已满', backpack_count=len(role.backpack))
    return False, None


def safe_wall(state, point, blocked, assignments):
    # 不把任何操控者封在无法返回其武器的位置；忽略可移动队友的临时占位。
    actors = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')]
    obstacles = blocked - {(r.pos.x, r.pos.y) for r in actors}
    obstacles = obstacles | {point}
    if not all(station_path(r, assignments[r.id], obstacles, state) is not None
               for r in actors if r.id in assignments):
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
        WEAPON_TYPES, WANTED_WEAPONS, decide_self_heal, decide_shop_item_job, item_cost,
        maybe_start_shop_item_job, own_station, plan_pioneer_tasks, pick_weapon_name,
        should_upgrade_weapon,
    )
    from .economy import liquidate, muster_for_night, profitable_mine
    from copy import copy
    base = own_station(state)
    if base is None:
        return {}
    fighters = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')]
    workers = sorted((r for r in fighters if r.role_type == 'worker'), key=lambda r: r.id)
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES]
    ring = staged_wall_plan(state, base)
    existing_walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'}
    missing = [p for p in ring if p not in existing_walls]
    blocked, reserved = build_blocked_set(state) | movement_avoid(state), set()
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    assignments = assign_weapons(state, excluded_ids=task_pioneers)
    remaining = 70 - state.round_no
    travel = [station_path(r, assignments[r.id], blocked - {(a.pos.x, a.pos.y) for a in fighters}, state)
              for r in fighters if r.id in assignments]
    muster_need = max([len(p) for p in travel if p is not None] + [0]) + MUSTER_BUFFER
    muster = bool(weapons) and remaining <= muster_need
    upgraded_once = any((w.level or 1) >= 2 for w in weapons)
    has_three = len(weapons) >= 3
    gold, builds = state.team_our.gold_num, 0
    budget = opening_time_budget(state, missing, remaining, muster_need, gold, upgraded_once, blocked)
    allow_walls = has_three and budget['allow_walls']
    allow_upgrade = has_three and budget['allow_upgrade']
    allow_sell = has_three and budget['allow_sell']
    allow_mine = has_three and budget['allow_mine']
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
          geometry_note='先三座火箭，再升最前一门，迎敌墙约8段；格子合法性由执行反馈确认',
          rounds_to_night=remaining,
          attack_from='右侧' if attack_direction(state, base) == 1 else '左侧', direction_source='用户确认的刷新规则')
    trace(state, None, 'opening_time_budget', '按剩余回合判断能否买券并修完7-8段墙',
          remaining=remaining, wall_need=budget['wall_need'], wall_deadline=budget['wall_deadline'],
          sell_trip=budget['sell_trip'], allow_walls=allow_walls, allow_upgrade=allow_upgrade,
          allow_sell=allow_sell, gold=gold, upgraded=upgraded_once)
    trace(state, None, 'funnel_layout', '实验性双层防线；外层留口，己方从后方通行',
          gap=funnel_gap(state, base), layers=2 if outer_wall_ready(state) and funnel_gap(state, base) else 1,
          outer_unlocked=outer_wall_ready(state),
          effect_note='机器人可能直接攻击墙，分流效果需回放验证')
    claimed = set()
    # 开拓者先规划撤离，避免继续占住墙线和工人施工邻接格。
    for role in sorted(fighters, key=lambda r: (r.role_type != 'pioneer', r.id)):
        if role.id in task_pioneers:
            continue
        handled, cmd = muster_for_night(role, state, blocked, reserved)
        if handled:
            if cmd:
                commands[role.id] = cmd
            continue
        budget_state = copy(state)
        budget_state.team_our = copy(state.team_our)
        budget_state.team_our.gold_num = gold
        trace(state, role.id, 'opening_rockets_first', '首日先三座火箭，再筹资升最前一门，再补迎敌7-8段墙')
        if has_three and not muster:
            if allow_upgrade:
                cmd = decide_shop_item_job(role, budget_state, blocked, reserved)
                if not cmd and should_upgrade_weapon(budget_state):
                    maybe_start_shop_item_job(role, budget_state)
                    cmd = decide_shop_item_job(role, budget_state, blocked, reserved)
                if cmd:
                    if cmd['action'] == 'buy':
                        gold -= item_cost(cmd['name'], state)
                    commands[role.id] = cmd
                    continue
            if allow_sell and any(z.neutral_type == 'vendor' for z in state.map_info.zones):
                handled, cmd = liquidate(role, budget_state, blocked, reserved)
                if handled:
                    if cmd:
                        commands[role.id] = cmd
                    continue
            if allow_mine and not allow_walls:
                cmd = profitable_mine(role, budget_state, blocked, reserved)
                if cmd:
                    commands[role.id] = cmd
                    continue
        if muster or (has_three and allow_walls and not missing):
            weapon = assignments.get(role.id)
            if weapon:
                trace(state, role.id, 'weapon_assignment', '夜间一人一炮，提前就位', weapon_id=weapon.id)
                walkable = (blocked - {(r.pos.x, r.pos.y) for r in fighters}) | reserved
                cmd = move_on_path(state, role, station_path(role, weapon, walkable, state), reserved, '前往分配武器')
                if cmd:
                    commands[role.id] = cmd
            continue
        if role.role_type == 'pioneer':
            # 停靠在墙线内部，远离其他角色、武器候选圈与工人的已计划目标。
            left, right, bottom, top = defense_bounds(state, base)
            goals = {(x, y) for x in range(left+1, right) for y in range(bottom+1, top)
                     if (x, y) not in blocked | reserved or (x, y) == (role.pos.x, role.pos.y)}
            hot = set()
            planned_pos = []
            for n in WANTED_WEAPONS:
                spots = weapon_candidates(state, base, n, extra_positions=planned_pos)[:4]
                if spots:
                    planned_pos.append(spots[0])
                hot.update(spots)
                for sx, sy in spots:
                    hot.update((p.x, p.y) for p in neighbors8(Pos(sx, sy), state.map_info.width, state.map_info.height))
            goals = {p for p in goals if p not in hot}
            goals = {p for p in goals if all(chebyshev(Pos(*p), w.pos) > 1 for w in workers)} or goals
            if not goals:
                continue
            path = path_to_any(role.pos, goals, blocked | reserved, state.map_info.width, state.map_info.height)
            cmd = move_on_path(state, role, path, reserved, '开拓者退出墙线并在基地内侧避让施工')
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
                mines = sorted((z for z in state.map_info.zones if z.neutral_type == 'stone'),
                               key=lambda z: chebyshev(role.pos, z.pos))
                for mine in mines:
                    path = adjacent_path(role, mine.pos, blocked | reserved, state)
                    if path is None:
                        continue
                    if not path and len(role.backpack) < role.back_pack_capability:
                        commands[role.id] = selected(
                            state, role.id,
                            {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]},
                            '为连续建墙批量采石')
                    elif path and len(role.backpack) < role.back_pack_capability:
                        commands[role.id] = move_on_path(state, role, path, reserved, '前往可达石矿准备建墙材料')
                    break
                if role.id in commands:
                    continue
            if stones == 0:
                trace(state, role.id, 'wall_no_stone', '没有石头，且没有可执行的采石行动')
                continue
        else:
            cmd = profitable_mine(role, budget_state, blocked, reserved)
            if cmd:
                commands[role.id] = cmd
            continue
        if kind == 'wall':
            candidates = sorted(candidates, key=lambda p: (wall_priority(state, base, p), chebyshev(role.pos, Pos(*p)), p))
        for point in candidates:
            if point in blocked | reserved | claimed or (*point, kind) in state.failed_build_spots:
                continue
            if kind == 'wall' and not safe_wall(state, point, blocked | claimed, assignments):
                continue
            path = adjacent_path(role, Pos(*point), blocked | reserved, state)
            if path is None:
                continue
            claimed.add(point)
            if kind == 'weapon':
                builds += 1
            if path:
                cmd = move_on_path(state, role, path, reserved, '前往武器施工位' if kind == 'weapon' else '优先补齐迎敌正面，其次侧翼')
            else:
                name = pick_weapon_name(state, [c.get('name') for c in commands.values() if c.get('action') == 'build']) if kind == 'weapon' else 'wall'
                cmd = selected(state, role.id, {'action': 'build', 'name': name, 'targetPos': [{'x': point[0], 'y': point[1]}]}, '建造武器' if kind == 'weapon' else '建造迎敌防线')
                reserved.add(point)
                if kind == 'weapon':
                    gold -= 25
                else:
                    blocked.add(point)
            if cmd:
                commands[role.id] = cmd
            break
        else:
            trace(state, role.id, 'opening_no_candidate', '候选位置被占用、不可达、处于失败冷却或会封住返程；未完成墙线不会标为完成', kind=kind)
            if kind == 'wall' and role.id in assignments:
                path = station_path(role, assignments[role.id], blocked | reserved, state)
                cmd = move_on_path(state, role, path, reserved, '先返回墙内，准备从内侧封闭最后缺口')
                if cmd:
                    commands[role.id] = cmd
    for role in fighters:
        if role.id not in commands:
            heal = decide_self_heal(role)
            if heal:
                commands[role.id] = selected(state, role.id, heal, '没有更高优先级行动，最后执行自救')
    return commands
