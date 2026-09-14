"""主动变现策略。阈值是可调策略参数，商品价格优先使用当前快照。"""
from collections import Counter

from .protocol import Pos, Role
from .grid import chebyshev
from .decision_log import trace, selected

SELL_VALUE = 10
SELL_COUNT = 6
SELL_FILL_RATIO = 0.20
BATCH_FILL_RATIO = 0.50  # 不急用时背包至少一半再跑小贩，避免采一点卖一点。
NEAR_CAP_SLOTS = 2
WORKER_WALL_OPPORTUNITY = 6
PIONEER_TASK_OPPORTUNITY = 8
BUILD_STONE_RESERVE = 4
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


def worker_should_shop_weapon_voucher(role, state, blocked=None):
    """工人买券：本人已持券，或完整代价比较后轮到这名工人。"""
    from .brain import weapon_upgrade_due
    if role.role_type != 'worker':
        return False
    if any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in role.backpack):
        return True
    if not weapon_upgrade_due(state) and not any(
            job.get('kind') == 'weapon' for job in state.worker_item_jobs.values()):
        return False
    buyer = pick_weapon_voucher_buyer(state, blocked)
    return bool(buyer and buyer.id == role.id)


def voucher_funding_gap(state):
    from .brain import weapon_upgrade_due
    if not weapon_upgrade_due(state) and not any(
            job.get('kind') == 'weapon' for job in state.worker_item_jobs.values()):
        return 0
    return max(0, next_weapon_voucher_cost(state) - (state.team_our.gold_num if state.team_our else 0))


def _voucher_holder(role):
    return any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in role.backpack)


def _upgrade_target_weapon(state):
    from .brain import WEAPON_TYPES, _pick_upgradeable, _pending_item_job_targets
    job = next((j for j in state.worker_item_jobs.values() if j.get('kind') == 'weapon'), None)
    if job:
        x, y = job['target']
        weapon = next((r for r in state.team_our.roles
                       if r.role_type in WEAPON_TYPES and r.pos.x == x and r.pos.y == y), None)
        if weapon:
            return weapon
    return _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=2)


def _voucher_opportunity(role, state, at_shop=False):
    if at_shop:
        return 0
    if role.role_type == 'worker':
        from .opening import critical_wall_missing
        return WORKER_WALL_OPPORTUNITY if critical_wall_missing(state) else 0
    if role.role_type != 'pioneer' or not state.team_our:
        return 0
    tasks = [t for t in state.team_our.player_tasks
             if t.task_type in ('自进化类1', '自进化类2') and t.is_valid and t.cold_down_rounds == 0]
    if not tasks:
        return 0
    nearest = min(chebyshev(role.pos, t.task_position) for t in tasks)
    return PIONEER_TASK_OPPORTUNITY if nearest <= 8 else PIONEER_TASK_OPPORTUNITY // 2


def _voucher_trip_parts(role, state, blocked, weapon, cost):
    """返回(实际回合, 综合代价)；走不通或钱凑不够则 None。"""
    from .brain import find_zone
    from .opening import adjacent_path, mobile_walkable
    blocked = mobile_walkable(state, blocked, set())
    if weapon is None:
        return None
    if _voucher_holder(role):
        path = adjacent_path(role, weapon.pos, blocked, state)
        if path is None:
            return None
        time_needed = len(path) + 1
        return time_needed, time_needed + _voucher_opportunity(role, state)
    gold = state.team_our.gold_num if state.team_our else 0
    sell_extra = 0
    start = role
    if gold < cost:
        if role.role_type != 'worker':
            return None
        gap = cost - gold
        if backpack_ore_value(role, state) < gap:
            return None
        ores = sellable_ores(role, state)
        vendors = [z.pos for z in state.map_info.zones if z.neutral_type == 'vendor']
        vendor_choices = []
        for pos in vendors:
            path = adjacent_path(role, pos, blocked, state)
            if path is not None:
                vendor_choices.append((len(path), path, pos))
        if not vendor_choices:
            return None
        _, vendor_path, vendor_pos = min(vendor_choices, key=lambda c: (c[0], c[2].x, c[2].y))
        sell_extra = len(vendor_path) + max(1, len(ores))
        sell_at = vendor_path[-1] if vendor_path else vendor_pos
        start = Role(role.id, sell_at, role.role_type, role.health)
    shop = find_zone(state, 'weaponShop')
    if shop is None:
        return None
    if chebyshev(start.pos, shop.pos) <= 1:
        shop_steps = 0
        to_shop = []
        at_shop = chebyshev(role.pos, shop.pos) <= 1 and gold >= cost
    else:
        to_shop = adjacent_path(start, shop.pos, blocked, state)
        if to_shop is None:
            return None
        shop_steps = len(to_shop)
        at_shop = False
    shop_stand = start.pos if not to_shop else to_shop[-1]
    stand = Role(role.id, shop_stand, role.role_type, role.health)
    use_path = adjacent_path(stand, weapon.pos, blocked, state)
    if use_path is None:
        return None
    time_needed = sell_extra + shop_steps + 1 + len(use_path) + 1
    return time_needed, time_needed + _voucher_opportunity(role, state, at_shop=at_shop)


def pick_weapon_voucher_buyer(state, blocked=None):
    """在能按时完成的人里选综合代价最低的；已持券优先。执行中任务保持稳定，除非阵亡、不可达或赶不上截止。"""
    if not state.team_our or not state.map_info:
        return None
    from .brain import item_cost, voucher_for, weapon_upgrade_due
    from .grid import build_blocked_set
    from .opening import MUSTER_BUFFER, mobile_walkable, movement_avoid, station_return_steps
    from .tactics import night_wave_cleared, threat_eta_to_base
    if blocked is None:
        blocked = build_blocked_set(state) | movement_avoid(state)
    blocked = mobile_walkable(state, blocked, set())
    weapon = _upgrade_target_weapon(state)
    name, _ = voucher_for('weapon', (weapon.level or 1) if weapon else 1)
    cost = item_cost(name, state)
    arrival = None if night_wave_cleared(state) else threat_eta_to_base(state)

    def still_ok(role):
        if role is None or role.health <= 0:
            return False
        parts = _voucher_trip_parts(role, state, blocked, weapon, cost)
        if parts is None:
            return False
        time_needed, _ = parts
        gun_back = station_return_steps(role, state, blocked, from_pos=weapon.pos if weapon else None)
        if gun_back is None:
            return False
        total = time_needed + gun_back
        if arrival is not None and total + MUSTER_BUFFER >= arrival:
            return False
        return True

    existing = next((j for j in state.worker_item_jobs.values() if j.get('kind') == 'weapon'), None)
    if existing:
        owner = next((r for r in state.team_our.roles
                      if r.id in state.worker_item_jobs
                      and state.worker_item_jobs[r.id].get('kind') == 'weapon'
                      and r.health > 0), None)
        if still_ok(owner):
            return owner
    if not weapon_upgrade_due(state) and not existing:
        return None
    if weapon is None:
        return None
    best = None
    for role in state.team_our.roles:
        if role.role_type not in ('worker', 'pioneer') or role.health <= 0:
            continue
        if role.role_type == 'pioneer' and state.phase_task:
            continue
        parts = _voucher_trip_parts(role, state, blocked, weapon, cost)
        if parts is None:
            continue
        time_needed, score = parts
        gun_back = station_return_steps(role, state, blocked, from_pos=weapon.pos)
        if gun_back is None:
            continue
        total = time_needed + gun_back
        if arrival is not None and total + MUSTER_BUFFER >= arrival:
            continue
        holder = 0 if _voucher_holder(role) else 1
        role_rank = 0 if role.role_type == 'pioneer' else 1
        key = (holder, score + gun_back, total, role_rank, role.id)
        if best is None or key < best[0]:
            best = (key, role, total, score)
    if best is None:
        return None
    _, role, time_needed, score = best
    trace(state, role.id, 'voucher_buyer_pick', '按卖矿绕路、到店、使用和回炮的完整代价派人买券',
          time_needed=time_needed, score=score, weapon_id=weapon.id, required_gold=cost)
    return role


def defense_due(role, state, blocked):
    """安全余量不足则回防。高压和正在受攻击优先于历史空窗；找不到回路则停止新外出。"""
    from .opening import MUSTER_BUFFER, station_return_steps
    from .brain import is_day_round
    from .tactics import imminent_contact, night_wave_cleared, pressure, threat_eta_to_base
    if pressure(state) or imminent_contact(state):
        return True
    travel = station_return_steps(role, state, blocked)
    if travel is None:
        return True
    if night_wave_cleared(state):
        return False
    if not is_day_round(state.round_no):
        return True
    arrival = threat_eta_to_base(state)
    if arrival is None:
        return True
    return travel + MUSTER_BUFFER >= arrival


def task_defense_override(state) -> bool:
    """粗门控仍保留给日志对照：True=防守应覆盖任务。真正是否留在任务点看 pioneer_should_hold_task。"""
    if (state.round_no or 0) >= THIRD_NIGHT_ROUND:
        return True
    from .brain import WEAPON_TYPES
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    return not weapons or any((w.level or 1) < 2 for w in weapons)


def solver_can_progress(state) -> bool:
    session = getattr(state, 'task_session', None) or {}
    return bool(state.phase_task) and session.get('stage') != 'exhausted'


def solver_ready_to_submit(state) -> bool:
    session = getattr(state, 'task_session', None) or {}
    if session.get('stage') in ('submit', 'wait_submit'):
        return True
    return bool(session.get('answer'))


def pioneer_should_hold_task(pioneer, state) -> bool:
    """回防窗里只有「本回合能提交且敌人还来不及打到」或「两门炮能守住当前可见波次且还能推进题目」才留在任务点。
    三炮二级不能单独推出少一人防守。无法推进则回炮，解题会话本身不清。"""
    if pioneer is None or pioneer.health <= 0 or not state.phase_task:
        return False
    from .tactics import pressure, threat_eta_to_base, two_guns_can_hold
    if pressure(state):
        return False
    eta = threat_eta_to_base(state)
    from .grid import build_blocked_set
    from .opening import MUSTER_BUFFER, movement_avoid, station_return_steps
    blocked = build_blocked_set(state) | movement_avoid(state)
    if solver_ready_to_submit(state):
        back = station_return_steps(pioneer, state, blocked)
        if back is None:
            return False
        need = 1 + back + MUSTER_BUFFER
        if eta is None or eta > need:
            return True
        return False
    if not solver_can_progress(state):
        return False
    return two_guns_can_hold(state)


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
    if role.role_type == 'pioneer' and pioneer_should_hold_task(role, state):
        return False, None
    weapon = assign_weapons(state).get(role.id)
    if weapon is None:
        from .brain import own_station
        from .opening import adjacent_path
        base = own_station(state)
        path = adjacent_path(role, base.pos, blocked | reserved, state) if base else None
        trace(state, role.id, 'no_free_weapon', '进入回防时段但缺少独立武器，先返回基地')
        return True, move_on_path(state, role, path, reserved, '没有武器也不留在外面，返回基地')
    path = station_path(role, weapon, blocked | reserved, state)
    remaining = 70 - cycle if cycle < 70 else 0
    from .tactics import threat_eta_to_base
    arrival = threat_eta_to_base(state)
    trace(state, role.id, 'income_muster', '安全余量不足，提前回到分配武器', weapon_id=weapon.id,
          remaining_day_rounds=remaining, threat_eta=arrival,
          return_steps=None if path is None else len(path))
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
    vendors = [z for z in state.map_info.zones if z.neutral_type == 'vendor']
    adjacent = any(chebyshev(role.pos, z.pos) <= 1 for z in vendors)
    triggers = []
    from .brain import should_upgrade_weapon
    waiting_weapon_job = any(job.get('kind') == 'weapon' for job in state.worker_item_jobs.values())
    need_voucher = should_upgrade_weapon(state) or waiting_weapon_job
    gap = voucher_funding_gap(state)
    cap = role.back_pack_capability or 0
    fill = (len(role.backpack) / cap) if cap else 1.0
    from .world_intel import ores_in_spike
    spiked = bool(ores_in_spike(state) & set(ores))
    if role.role_type == 'worker' and worker_should_shop_weapon_voucher(role, state) and gap and value >= gap:
        triggers.append('卖掉本包后工人去买武器升级券')
    if (state.round_no or 0) < 70:
        if need_voucher and gap and state.team_our.gold_num + value >= next_weapon_voucher_cost(state):
            triggers.append('现金加本包估值已够本次必要升级券')
        elif not triggers and role.id not in committed:
            return False, None
    else:
        if gap and value >= gap:
            triggers.append('卖掉本包即可完成必要武器升级')
        if role.health < max_health(role) * 0.6:
            triggers.append('低血量携矿风险')
        if spiked:
            triggers.append('官方消息涨价窗口，优先卖出对应矿石')
        if fill >= BATCH_FILL_RATIO:
            triggers.append('背包过半，批量变现')
        if cap and cap - len(role.backpack) <= NEAR_CAP_SLOTS:
            triggers.append('背包即将满载，批量变现')
    choices = []
    for vendor in vendors:
        path = adjacent_path(role, vendor.pos, blocked | reserved, state)
        if path is not None:
            choices.append((len(path), vendor.pos.x, vendor.pos.y, vendor, path))
    if not choices:
        if not (triggers or adjacent or role.id in committed):
            return False, None
        trace(state, role.id, 'sale_unreachable', '需要变现，但当前没有可达的小贩；不继续盲目采矿', ore_value=value)
        return True, None
    _, _, _, vendor, path = min(choices, key=lambda c: c[:3])
    sale_rounds = 0
    arrival = None
    if path:
        from .opening import MUSTER_BUFFER
        from .tactics import night_wave_cleared, threat_eta_to_base
        weapons = [r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        selling_pos = path[-1]
        proxy = Role(-1, selling_pos, 'worker', 1)
        obstacles = blocked - {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')}
        return_paths = [adjacent_path(proxy, w.pos, obstacles, state) for w in weapons]
        return_lengths = [len(p) for p in return_paths if p is not None]
        return_time = min(return_lengths) if return_lengths else (0 if not weapons else 10000)
        sale_rounds = len(path) + len(ores) + return_time + MUSTER_BUFFER
        if return_time >= 10000:
            trace(state, role.id, 'sale_too_late', '卖完后找不到回路，不批准这趟外出',
                  estimated_rounds=sale_rounds, return_time=return_time)
            return False, None
        if not night_wave_cleared(state):
            arrival = threat_eta_to_base(state)
            if arrival is None or sale_rounds >= arrival:
                trace(state, role.id, 'sale_too_late', '卖矿往返将吃掉安全余量，暂停外出',
                      estimated_rounds=sale_rounds, threat_eta=arrival)
                return False, None
            if (state.round_no or 0) >= 70 and not (triggers or adjacent or role.id in committed):
                extra = min(cap - len(role.backpack), 6) if cap else 6
                if extra > 0 and sale_rounds + extra >= arrival and value > 0:
                    triggers.append('再采一趟将错过回防前变现')
                else:
                    return False, None
        elif not (triggers or adjacent or role.id in committed):
            return False, None
    elif not (triggers or adjacent or role.id in committed):
        return False, None
    if role.id not in committed:
        committed.append(role.id)
    trace(state, role.id, 'cashout_priority', '急用立即变现，否则等批量再跑小贩', triggers=triggers,
          sellable=dict(ores), quoted_value=value, known_prices=prices,
          stone_reserved=role.backpack.count('stone')-ores.get('stone', 0),
          trip_rounds=sale_rounds, threat_eta=arrival, fill_ratio=round(fill, 2))
    if not path:
        name = max(ores, key=lambda n: (ores[n] * prices.get(n, 0), ores[n], n))
        return True, selected(state, role.id, {'action': 'sell', 'name': name, 'num': ores[name]}, '批量出售同种矿石，减少往返')
    return True, move_on_path(state, role, path, reserved, '本趟批量变现，不采一点卖一点')


def profitable_mine(role, state, blocked, reserved):
    """按本趟实际能采的数量估算收益：受背包剩余格约束，不再固定按10次采集。仅工人可 collect。"""
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
    batch = max(1, (role.back_pack_capability or 1) - len(role.backpack))
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
        score = batch * prices.get(mine.neutral_type, 1) / (len(path) + batch + return_distance + 1)
        if mine.neutral_type in ores_to_stockpile(state):
            score *= 3
        candidates.append((score, -len(path), mine, path))
    if not candidates:
        trace(state, role.id, 'no_reachable_mine', '当前没有可达矿点')
        return None
    _, _, mine, path = max(candidates, key=lambda c: c[:2])
    trace(state, role.id, 'income_mine', '按本趟可装容量、报价与运输成本估算矿点收益', mineral=mine.neutral_type,
          price=prices.get(mine.neutral_type), batch=batch,
          estimate_note='未知报价按等权比较；返售距离是几何估计')
    if path:
        return move_on_path(state, role, path, reserved, '前往本趟批量收益较高的可达矿点')
    return selected(state, role.id, {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]}, '采集矿石，凑够一趟再出售')
