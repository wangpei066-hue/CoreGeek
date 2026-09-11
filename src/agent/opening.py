"""第一天：迎敌城墙 -> 三座武器 -> 分别就位；不启动卖矿经济。

墙线是候选几何规划，不是官方合法区域；以快照中的建筑判断完成。
"""
from collections import deque
from itertools import permutations

from .protocol import Pos
from .grid import build_blocked_set, chebyshev, neighbors8
from .decision_log import trace, selected

WALL_MARGIN = 2
STONE_BATCH = 4  # 与经济策略的建墙石料预留一致，避免采满后又卖掉形成循环。
MUSTER_BUFFER = 3


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
    center_x = base.pos.x + 0.5
    cells = {(front, y) for y in range(bottom, top + 1)}
    gap = funnel_gap(state, base)
    if gap:
        cells.update((gap[0], y) for y in range(bottom, top + 1) if (gap[0], y) != gap)
    cells.update((x, y) for x in range(left, right + 1) for y in (bottom, top)
                 if (x - center_x) * direction > 0)
    return sorted(cells, key=lambda p: (wall_priority(state, base, p), p))


def assign_weapons(state, excluded_ids=()):
    """至多三座武器，枚举一对一分配，优先可达并最小化总路程。"""
    fighters = sorted((r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.id not in excluded_ids), key=lambda r: r.id)
    weapons = sorted((r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')), key=lambda r: r.id)
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
    from .brain import WEAPON_TYPES, decide_self_heal, item_cost, own_station, plan_pioneer_tasks
    base = own_station(state)
    if base is None:
        return {}
    fighters = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')]
    workers = sorted((r for r in fighters if r.role_type == 'worker'), key=lambda r: r.id)
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES]
    ring = wall_ring(state, base)
    existing_walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'}
    missing = [p for p in ring if p not in existing_walls]
    blocked, reserved = build_blocked_set(state) | movement_avoid(state), set()
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    assignments = assign_weapons(state, excluded_ids=task_pioneers)
    remaining = 70 - state.round_no
    travel = [station_path(r, assignments[r.id], blocked - {(a.pos.x, a.pos.y) for a in fighters}, state)
              for r in fighters if r.id in assignments]
    muster = bool(weapons) and (remaining <= max([len(p) for p in travel if p is not None] + [0]) + MUSTER_BUFFER)
    phase = '就位' if muster else ('围墙' if missing else ('武器' if len(weapons) < 3 else '升级'))
    trace(state, None, 'opening_phase', '第一天阶段计划', phase=phase, weapons=len(weapons),
          wall_goal=len(ring), walls_completed=len(ring)-len(missing), wall_missing=missing,
          geometry_note='正面优先、侧翼其次、后方开放；格子合法性由执行反馈确认', rounds_to_night=remaining,
          attack_from='右侧' if attack_direction(state, base) == 1 else '左侧', direction_source='用户确认的刷新规则')
    trace(state, None, 'funnel_layout', '实验性双层防线；外层留口，己方从后方通行',
          gap=funnel_gap(state, base), layers=2 if funnel_gap(state, base) else 1,
          effect_note='机器人可能直接攻击墙，分流效果需回放验证')
    gold, builds = state.team_our.gold_num, 0  # 保留plan_pioneer_tasks已经生成的指令。
    claimed = set()
    # 开拓者先规划撤离，避免继续占住墙线和工人施工邻接格。
    for role in sorted(fighters, key=lambda r: (r.role_type != 'pioneer', r.id)):
        if role.id in task_pioneers:
            continue
        heal = decide_self_heal(role)
        if heal:
            commands[role.id] = selected(state, role.id, heal, '低血量优先自救')
            continue
        from copy import copy
        budget_state = copy(state)
        budget_state.team_our = copy(state.team_our)
        budget_state.team_our.gold_num = gold
        trace(state, role.id, 'opening_defense_only', '首日先建城墙，再补武器；不启动卖矿、收益采矿或进攻购物')
        if not missing and not muster and len(weapons) >= 3:
            from copy import copy
            from .brain import maybe_start_shop_item_job, decide_shop_item_job
            budget_state = copy(state)
            budget_state.team_our = copy(state.team_our)
            budget_state.team_our.gold_num = gold
            maybe_start_shop_item_job(role, budget_state)
            cmd = decide_shop_item_job(role, budget_state, blocked, reserved)
            if cmd:
                if cmd['action'] == 'buy':
                    gold -= item_cost(cmd['name'], state)
                commands[role.id] = cmd
                continue
        if muster or (not missing and len(weapons) >= 3):
            weapon = assignments.get(role.id)
            if weapon:
                trace(state, role.id, 'weapon_assignment', '夜间一人一炮，提前就位', weapon_id=weapon.id)
                cmd = move_on_path(state, role, station_path(role, weapon, blocked | reserved, state), reserved, '前往分配武器')
                if cmd:
                    commands[role.id] = cmd
            continue
        if role.role_type == 'pioneer':
            # 停靠在墙线内部，远离其他角色、武器候选圈与工人的已计划目标。
            left, right, bottom, top = defense_bounds(state, base)
            goals = {(x, y) for x in range(left+1, right) for y in range(bottom+1, top)
                     if (x, y) not in blocked | reserved or (x, y) == (role.pos.x, role.pos.y)}
            # 优先基地旁、避开正在使用的工人交互位置。
            goals = {p for p in goals if all(chebyshev(Pos(*p), w.pos) > 1 for w in workers)} or goals
            path = path_to_any(role.pos, goals, blocked | reserved, state.map_info.width, state.map_info.height)
            cmd = move_on_path(state, role, path, reserved, '开拓者退出墙线并在基地内侧避让施工')
            if cmd:
                commands[role.id] = cmd
            continue
        if not missing and len(weapons) + builds < 3:
            if gold < 25:
                trace(state, role.id, 'opening_no_gold', '武器资金不足；首日不切换到卖矿流程，等待资金或第二天变现')
                continue
            # 三种武器置于墙线内侧；不在未来墙位上试建。
            candidates = [(x, y) for x in range(base.pos.x-1, base.pos.x+3)
                          for y in range(base.pos.y-2, base.pos.y+2)
                          if 0 <= x < state.map_info.width and 0 <= y < state.map_info.height]
            kind = 'weapon'
        elif not missing and len(weapons) < 3:
            trace(state, role.id, 'await_weapons', '等待本回合武器建造结果，不提前转入围墙')
            continue
        else:
            kind, candidates = 'wall', missing
            stones = role.backpack.count('stone')
            at_stone = any(z.neutral_type == 'stone' and chebyshev(role.pos, z.pos) <= 1 for z in state.map_info.zones)
            if stones == 0 or (at_stone and stones < min(STONE_BATCH, (len(missing)+1)//2) and remaining > 12):
                mines = sorted((z for z in state.map_info.zones if z.neutral_type == 'stone'), key=lambda z: chebyshev(role.pos, z.pos))
                for mine in mines:
                    path = adjacent_path(role, mine.pos, blocked | reserved, state)
                    if path is None:
                        continue
                    if not path and len(role.backpack) < role.back_pack_capability:
                        commands[role.id] = selected(state, role.id, {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]}, '为连续建墙批量采石，石头不出售')
                    elif path and len(role.backpack) < role.back_pack_capability:
                        commands[role.id] = move_on_path(state, role, path, reserved, '前往可达石矿准备建墙材料')
                    break
                if role.id in commands:
                    continue
            if stones == 0:
                trace(state, role.id, 'wall_no_stone', '没有石头，且没有可执行的采石行动')
                continue
        candidates = sorted(candidates, key=lambda p: (wall_priority(state, base, p) if kind == 'wall' else 0, chebyshev(role.pos, Pos(*p)), p))
        for point in candidates:
            if point in blocked | reserved | claimed or (*point, kind) in state.failed_build_spots:
                continue
            if kind == 'wall' and not safe_wall(state, point, blocked | claimed, assignments):
                continue
            path = adjacent_path(role, Pos(*point), blocked | reserved, state)
            if path is None:
                continue
            claimed.add(point)
            if path:
                cmd = move_on_path(state, role, path, reserved, '前往武器施工位' if kind == 'weapon' else '优先补齐迎敌正面，其次侧翼')
            else:
                name = next((t for t in WEAPON_TYPES if t not in [w.role_type for w in weapons]
                             and t not in [c.get('name') for c in commands.values()]), 'gatling') if kind == 'weapon' else 'wall'
                cmd = selected(state, role.id, {'action': 'build', 'name': name, 'targetPos': [{'x': point[0], 'y': point[1]}]}, '建造武器' if kind == 'weapon' else '建造迎敌防线')
                reserved.add(point)
                if kind == 'weapon':
                    gold -= 25
                    builds += 1
                else:
                    blocked.add(point)
            if cmd:
                commands[role.id] = cmd
            break
        else:
            trace(state, role.id, 'opening_no_candidate', '候选位置被占用、不可达、处于失败冷却或会封住返程；未完成墙线不会标为完成', kind=kind)
            # 最后缺口不能从外侧封死：先回到墙内武器旁，再从内侧封口。
            if kind == 'wall' and role.id in assignments:
                path = station_path(role, assignments[role.id], blocked | reserved, state)
                cmd = move_on_path(state, role, path, reserved, '先返回墙内，准备从内侧封闭最后缺口')
                if cmd:
                    commands[role.id] = cmd
    return commands
