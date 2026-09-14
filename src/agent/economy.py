"""主动变现策略。阈值是可调策略参数，商品价格优先使用当前快照。"""
from collections import Counter

from .protocol import Pos, Role
from .grid import chebyshev
from .decision_log import trace, selected

SELL_VALUE = 10
SELL_COUNT = 6
SELL_FILL_RATIO = 0.20
BUILD_STONE_RESERVE = 4
VOUCHER_FUND_TARGET = 130


THIRD_NIGHT_ROUND = 330  # 第三天夜晚起点（round_no 从0起算的假设下）。


def live_pioneer(state):
    return next((r for r in (state.team_our.roles if state.team_our else [])
                 if r.role_type == 'pioneer' and r.health > 0), None)


def pioneer_available_to_buy_voucher(state):
    """进行中的任务不中断；空闲开拓者才去买券。"""
    pioneer = live_pioneer(state)
    return bool(pioneer and not state.phase_task)


def next_weapon_voucher_cost(state):
    from .brain import item_cost, voucher_for, WEAPON_TYPES, _pick_upgradeable
    weapon = _pick_upgradeable(state, WEAPON_TYPES, set(), max_current_level=2)
    if weapon is None:
        return item_cost('WeaponUpgradeVoucher1', state)
    name, _ = voucher_for('weapon', weapon.level or 1)
    return item_cost(name, state)


def backpack_ore_value(role, state):
    ores = sellable_ores(role, state)
    prices = ore_prices(state)
    return sum(prices.get(name, 0) * count for name, count in ores.items())


def worker_should_shop_weapon_voucher(role, state):
    """工人买券：开拓者空闲且金币已够时让开拓者买；否则背包估值够缺口或金币已够则工人去买。"""
    from .brain import should_upgrade_weapon
    if role.role_type != 'worker' or not should_upgrade_weapon(state):
        return False
    cost = next_weapon_voucher_cost(state)
    if pioneer_available_to_buy_voucher(state) and state.team_our.gold_num >= cost:
        return False
    if any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in role.backpack):
        return True
    if state.team_our.gold_num >= cost:
        return True
    return backpack_ore_value(role, state) >= cost - state.team_our.gold_num


def defense_due(role, state, blocked):
    """夜间、白天第50回合或返程余量不足时，防守覆盖任务与经济。"""
    from .opening import assign_weapons, station_path, staged_walls_incomplete
    from .tactics import night_wave_cleared, pressure
    if night_wave_cleared(state):
        return False
    cycle = (state.round_no or 0) % 130
    if pressure(state):
        return True
    if cycle >= 50:
        # 第一晚后工人若还要攒石/末段建墙，不按第50回合一刀切回防。
        if (role.role_type == 'worker' and (state.round_no or 0) >= 70
                and staged_walls_incomplete(state)):
            pass
        else:
            return True
    weapon = assign_weapons(state).get(role.id)
    if weapon is None:
        return False
    path = station_path(role, weapon, blocked, state)
    return path is not None and len(path) + 8 >= 70 - cycle


def task_defense_override(state) -> bool:
    """用户确认的任务/防守优先级门控：返回 True 表示"防守优先，进行中的任务也要让路"（方案A）；
    False 表示"武器已全部升级到二级以上，且还没到第三夜，允许任务撑到自然结束"（方案B）。
    第三夜（round_no >= 330）起不再看武器状态，永远防守优先——生存权重高于任务，
    此时哪怕武器全满级也不再为任务让防守让路。"""
    if (state.round_no or 0) >= THIRD_NIGHT_ROUND:
        return True
    from .brain import WEAPON_TYPES
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    return not weapons or any((w.level or 1) < 2 for w in weapons)


def muster_for_night(role, state, blocked, reserved):
    """所有白天都按实际返程距离提前回防，而非仅首日集合。"""
    from .opening import assign_weapons, station_path, move_on_path
    from .tactics import night_wave_cleared, pressure
    if night_wave_cleared(state):
        return False, None
    cycle = (state.round_no or 0) % 130
    if (state.round_no or 0) < 70 and role.role_type == 'worker' and not pressure(state):
        return False, None  # 首日由施工计划按实际武器返程时间集合。
    if not defense_due(role, state, blocked):
        return False, None
    if role.role_type == 'pioneer' and state.phase_task and not task_defense_override(state):
        return False, None  # 方案B条件满足：回防时段已到，但武器已全部二级+、未到第三夜，任务继续，不被回防打断。
    weapon = assign_weapons(state).get(role.id)
    if weapon is None:
        from .brain import own_station
        from .opening import adjacent_path
        base = own_station(state)
        path = adjacent_path(role, base.pos, blocked | reserved, state) if base else None
        trace(state, role.id, 'no_free_weapon', '进入回防时段但缺少独立武器，先返回基地')
        return True, move_on_path(state, role, path, reserved, '没有武器也不留在外面，返回基地')
    path = station_path(role, weapon, blocked | reserved, state)
    # 临时受阻时仍停止向外采矿，下一回合重新寻路。
    trace(state, role.id, 'income_muster', '经济行动截止，提前回到分配武器等待夜战', weapon_id=weapon.id,
          remaining_day_rounds=70-cycle, return_steps=None if path is None else len(path))
    return True, move_on_path(state, role, path, reserved, '停止采矿和购物，提前回防')


def ore_prices(state):
    # 无报价时只按数量触发，不假装知道成交价格。
    return {i.name: max(0, i.price) for i in state.vendor_shop_list if i.name in ('stone', 'iron', 'copper')}


def sellable_ores(role, state):
    from .brain import own_station
    from .opening import staged_wall_plan
    ores = Counter(i for i in role.backpack if i in ('stone', 'iron', 'copper'))
    base = own_station(state)
    reserve = 0
    if base and role.role_type == 'worker':
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
        missing = len(set(staged_wall_plan(state, base)) - walls)
        workers = [r for r in state.team_our.roles if r.role_type == 'worker' and r.health > 0]
        hands = max(1, len(workers))
        from .brain import max_health
        if (state.round_no or 0) >= 70 and missing:
            # 第一晚后建墙用石全部留着，只卖超出缺口的部分。
            others = sum(r.backpack.count('stone') for r in workers if r.id != role.id)
            still_need = max(0, missing - others)
            reserve = min(ores['stone'], still_need)
        else:
            reserve = min(BUILD_STONE_RESERVE, (missing + hands - 1) // hands)
        if base.health < max_health(base) * 0.7:
            reserve = min(reserve, 1)
    ores['stone'] = max(0, ores['stone'] - reserve)
    from .world_intel import ores_in_spike, ores_to_stockpile
    backpack_tight = bool(role.back_pack_capability and len(role.backpack) >= role.back_pack_capability * 0.9)
    if not backpack_tight:
        spike = ores_in_spike(state)
        for name in ores_to_stockpile(state):
            if name not in spike:
                ores[name] = 0
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
    from .brain import item_cost, should_upgrade_weapon
    waiting_weapon_job = any(job.get('kind') == 'weapon' for job in state.worker_item_jobs.values())
    need_voucher = should_upgrade_weapon(state) or waiting_weapon_job
    voucher_need = item_cost('WeaponUpgradeVoucher1', state)
    if role.role_type == 'worker' and worker_should_shop_weapon_voucher(role, state):
        triggers.append('背包矿石估值已够工人去买武器升级券')
    if (state.round_no or 0) < 70:
        if need_voucher and state.team_our.gold_num < voucher_need and state.team_our.gold_num + value >= VOUCHER_FUND_TARGET:
            triggers.append('筹集约130金币购买武器升级券')
        elif not triggers and role.id not in committed:
            return False, None
    else:
        if value >= SELL_VALUE:
            triggers.append('可出售矿石估值达到10金币')
        if sum(ores.values()) >= SELL_COUNT:
            triggers.append('可出售矿石达到6个')
        if role.back_pack_capability and len(role.backpack) >= role.back_pack_capability * SELL_FILL_RATIO:
            triggers.append('背包达到20%')
        if role.health < max_health(role) * 0.6:
            triggers.append('低血量携矿风险')
        if 40 <= cycle_round < 70:
            triggers.append('天黑前提前变现')
        if 20 <= cycle_round < 50:
            triggers.append('白天中段提前清仓，为防守消费留时间')
        from .brain import own_station
        base = own_station(state)
        if base and base.health < max_health(base) * 0.8:
            triggers.append('基地受损，提前变现用于防守')
        if need_voucher and state.team_our.gold_num < voucher_need and state.team_our.gold_num + value >= VOUCHER_FUND_TARGET:
            triggers.append('筹集购买武器升级券')
        from .world_intel import ores_in_spike
        if ores_in_spike(state) & set(ores):
            triggers.append('官方消息涨价窗口，优先卖出对应矿石')
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
    """按一批10次采集的报价/行程估算选择可达矿点；不按纯距离挑矿。仅工人可 collect。"""
    from .opening import adjacent_path, move_on_path
    from .world_intel import ore_blocked, ores_to_stockpile
    if role.role_type != 'worker':
        trace(state, role.id, 'pioneer_cannot_collect', '采集仅工人可用，开拓者不采矿、不建墙')
        return None
    if len(role.backpack) >= role.back_pack_capability:
        trace(state, role.id, 'backpack_full', '背包已满，停止采矿')
        return None
    prices = ore_prices(state)
    vendors = [z for z in state.map_info.zones if z.neutral_type == 'vendor']
    from .opening import stones_cover_wall_plan
    skip_stone = stones_cover_wall_plan(state)
    candidates = []
    for mine in state.map_info.zones:
        if mine.neutral_type not in ('stone', 'iron', 'copper'):
            continue
        if skip_stone and mine.neutral_type == 'stone':
            continue
        if ore_blocked(state, mine.neutral_type):
            continue
        path = adjacent_path(role, mine.pos, blocked | reserved, state)
        if path is None:
            continue
        return_distance = min((chebyshev(mine.pos, v.pos) for v in vendors), default=0)
        score = 10 * prices.get(mine.neutral_type, 1) / (len(path) + 10 + return_distance + 1)
        if mine.neutral_type in ores_to_stockpile(state):
            score *= 3
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
