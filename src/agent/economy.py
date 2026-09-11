"""主动变现策略。阈值是可调策略参数，商品价格优先使用当前快照。"""
from collections import Counter

from .protocol import Pos, Role
from .grid import chebyshev
from .decision_log import trace, selected

SELL_VALUE = 25
SELL_COUNT = 12
SELL_FILL_RATIO = 0.35
BUILD_STONE_RESERVE = 4


def muster_for_night(role, state, blocked, reserved):
    """所有白天都按实际返程距离提前回防，而非仅首日集合。"""
    from .opening import assign_weapons, station_path, move_on_path
    cycle = (state.round_no or 0) % 130
    if not 40 <= cycle < 70:
        return False, None
    excluded = [r.id for r in state.team_our.roles if r.role_type == 'pioneer'
                and (state.phase_task or any(t.is_valid and t.cold_down_rounds == 0
                    and t.task_type in ('自进化类1', '自进化类2') for t in state.team_our.player_tasks))]
    weapon = assign_weapons(state, excluded).get(role.id)
    if weapon is None:
        return False, None
    path = station_path(role, weapon, blocked | reserved, state)
    # 临时受阻时仍停止向外采矿，下一回合重新寻路。
    if path is None or 70 - cycle <= len(path) + 5:
        trace(state, role.id, 'income_muster', '经济行动截止，提前回到分配武器等待夜战', weapon_id=weapon.id,
              remaining_day_rounds=70-cycle, return_steps=None if path is None else len(path))
        return True, move_on_path(state, role, path, reserved, '停止采矿和购物，提前回防')
    return False, None


def ore_prices(state):
    # 无报价时只按数量触发，不假装知道成交价格。
    return {i.name: max(0, i.price) for i in state.vendor_shop_list if i.name in ('stone', 'iron', 'copper')}


def sellable_ores(role, state):
    from .brain import own_station
    from .opening import wall_ring
    ores = Counter(i for i in role.backpack if i in ('stone', 'iron', 'copper'))
    base = own_station(state)
    reserve = 0
    if base and role.role_type == 'worker':
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'}
        missing = len(set(wall_ring(state, base)) - walls)
        workers = max(1, sum(r.role_type == 'worker' and r.health > 0 for r in state.team_our.roles))
        reserve = min(BUILD_STONE_RESERVE, (missing + workers - 1) // workers)
    ores['stone'] = max(0, ores['stone'] - reserve)
    return +ores


def liquidate(role, state, blocked, reserved):
    """返回(是否接管, 指令)。往返时间不足时停止外出，转入原有防守流程。"""
    from .brain import max_health
    from .opening import adjacent_path, move_on_path
    ores = sellable_ores(role, state)
    committed = state.policy_memory.setdefault('selling_roles', [])
    if not ores:
        if role.id in committed:
            committed.remove(role.id)
        return False, None
    prices = ore_prices(state)
    value = sum(prices.get(name, 0) * count for name, count in ores.items())
    cycle_round = (state.round_no or 0) % 130
    vendors = [z for z in state.map_info.zones if z.neutral_type == 'vendor']
    adjacent = any(chebyshev(role.pos, z.pos) <= 1 for z in vendors)
    triggers = []
    if value >= SELL_VALUE:
        triggers.append('可出售矿石估值达到25金币')
    if sum(ores.values()) >= SELL_COUNT:
        triggers.append('可出售矿石达到12个')
    if role.back_pack_capability and len(role.backpack) >= role.back_pack_capability * SELL_FILL_RATIO:
        triggers.append('背包达到35%')
    if role.health < max_health(role) * 0.6:
        triggers.append('低血量携矿风险')
    if 40 <= cycle_round < 70:
        triggers.append('天黑前提前变现')
    if not (triggers or adjacent or role.id in committed):
        return False, None
    choices = []
    for vendor in vendors:
        path = adjacent_path(role, vendor.pos, blocked | reserved, state)
        if path is not None:
            choices.append((len(path), vendor.pos.x, vendor.pos.y, vendor, path))
    if not choices:
        trace(state, role.id, 'sale_unreachable', '需要变现，但当前没有可达的小贩；不继续盲目采矿', ore_value=value)
        return True, None
    _, _, _, vendor, path = min(choices, key=lambda c: c[:3])
    if path:
        # 把出售多种矿石的回合数和返回武器所需时间也算进去。
        weapons = [r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        selling_pos = path[-1]
        proxy = Role(-1, selling_pos, 'worker', 1)
        obstacles = blocked - {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')}
        return_paths = [adjacent_path(proxy, w.pos, obstacles, state) for w in weapons]
        return_lengths = [len(p) for p in return_paths if p is not None]
        return_time = min(return_lengths) if return_lengths else (0 if not weapons else 10000)
        if cycle_round >= 70 or len(path) + len(ores) + return_time + 3 >= 70 - cycle_round:
            trace(state, role.id, 'sale_too_late', '卖矿往返将影响夜间防守，暂停外出', estimated_rounds=len(path)+len(ores)+return_time+3)
            return False, None
    if role.id not in committed:
        committed.append(role.id)
    trace(state, role.id, 'cashout_priority', '主动变现优先于建造、升级和继续采矿', triggers=triggers,
          sellable=dict(ores), quoted_value=value, known_prices=prices, stone_reserved=role.backpack.count('stone')-ores.get('stone', 0))
    if not path:
        name = max(ores, key=lambda n: (ores[n] * prices.get(n, 0), ores[n], n))
        return True, selected(state, role.id, {'action': 'sell', 'name': name, 'num': ores[name]}, '批量出售高价值矿石，完成变现任务')
    return True, move_on_path(state, role, path, reserved, '专程前往小贩变现，不等背包接近装满')


def profitable_mine(role, state, blocked, reserved):
    """按一批10次采集的报价/行程估算选择可达矿点；不按纯距离挑矿。"""
    from .opening import adjacent_path, move_on_path
    if len(role.backpack) >= role.back_pack_capability:
        trace(state, role.id, 'backpack_full', '背包已满，停止采矿')
        return None
    prices = ore_prices(state)
    vendors = [z for z in state.map_info.zones if z.neutral_type == 'vendor']
    candidates = []
    for mine in state.map_info.zones:
        if mine.neutral_type not in ('stone', 'iron', 'copper'):
            continue
        path = adjacent_path(role, mine.pos, blocked | reserved, state)
        if path is None:
            continue
        return_distance = min((chebyshev(mine.pos, v.pos) for v in vendors), default=0)
        score = 10 * prices.get(mine.neutral_type, 1) / (len(path) + 10 + return_distance + 1)
        candidates.append((score, -len(path), mine, path))
    if not candidates:
        trace(state, role.id, 'no_reachable_mine', '当前没有可达矿点')
        return None
    _, _, mine, path = max(candidates, key=lambda c: c[:2])
    trace(state, role.id, 'income_mine', '按报价、采集与运输成本估算矿点收益', mineral=mine.neutral_type,
          price=prices.get(mine.neutral_type), estimate_note='未知报价按等权比较；返售距离是几何估计')
    if path:
        return move_on_path(state, role, path, reserved, '前往预期收益较高的可达矿点')
    return selected(state, role.id, {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]}, '采集矿石用于近期出售')
