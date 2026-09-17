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
PRE_NIGHT_CASHOUT_LEAD = 12  # 卖掉之后还要留出买券/用券时间。
PRE_NIGHT3_CASHOUT_LEAD = 20  # 第三晚压力大，更早把背包换成火力。
MINE_TRIP_CAP = 6  # 本趟收益只按还能采的几下算，不用整包空位去抬远矿。
MINE_TARGETS_KEY = 'mine_targets'
SPIKE_CASHOUT_KEY = 'spike_cashout'
NIGHT_SELL_AFTER = 30  # 入夜满这么多回合后，夜里外出的工人才允许去小贩卖矿。
EN_ROUTE_SLACK = 2  # 顺路采矿后，回去用券还要再留的余量。


def en_route_collect(role, state, remaining_steps, reason):
    """带着升级道具回家时，路过的矿就在身边且入夜前仍来得及回去使用，就先采一下，
    免得升级后再专程跑回来。"""
    from .brain import is_day_round
    from .opening import MUSTER_BUFFER, day_rounds_remaining, staged_walls_incomplete
    if role.role_type != 'worker' or remaining_steps is None or not state.map_info:
        return None
    if not is_day_round(state.round_no):
        return None
    cap = role.back_pack_capability or 0
    if cap and len(role.backpack or []) >= cap:
        return None
    # 本回合采集 + 剩余路程 + 使用道具 + 回防余量
    need = 1 + remaining_steps + 1 + MUSTER_BUFFER + EN_ROUTE_SLACK
    if day_rounds_remaining(state.round_no) <= need:
        return None
    want = {'copper', 'iron'}
    if staged_walls_incomplete(state):
        want.add('stone')
    try:
        from .world_intel import ore_blocked
        want = {ore for ore in want if not ore_blocked(state, ore)}
    except Exception:
        pass
    mines = [z for z in state.map_info.zones
             if z.neutral_type in want and chebyshev(role.pos, z.pos) <= 1]
    if not mines:
        return None
    prices = ore_prices(state)
    mine = max(mines, key=lambda z: (prices.get(z.neutral_type, 0), -z.pos.x, -z.pos.y))
    trace(state, role.id, 'en_route_collect', reason, ore=mine.neutral_type,
          remaining_steps=remaining_steps, day_rounds_left=day_rounds_remaining(state.round_no), need=need)
    return selected(state, role.id,
                    {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]}, reason)


def price_spike_today(state):
    """官方消息里今天涨价的矿种。"""
    try:
        memory = getattr(state, 'news_memory', None)
        if memory is not None:
            from .news_memory import game_day
            return set(memory.price_boosted_ores(game_day(state.round_no)))
        from .world_intel import ores_in_spike
        return set(ores_in_spike(state))
    except Exception:
        return set()


def spike_cashout_phase(state):
    """矿价上涨日经济工的阶段：sell（清包）→ upgrade（买券升级）→ done（回去采矿）；非涨价日为None。"""
    from .brain import DAY_NIGHT_CYCLE, is_day_round
    if not is_day_round(state.round_no) or not price_spike_today(state):
        return None
    day = str(int(state.round_no or 0) // DAY_NIGHT_CYCLE)
    return (state.policy_memory.get(SPIKE_CASHOUT_KEY) or {}).get(day, 'sell')


def set_spike_cashout_phase(state, phase):
    from .brain import DAY_NIGHT_CYCLE
    day = str(int(state.round_no or 0) // DAY_NIGHT_CYCLE)
    state.policy_memory[SPIKE_CASHOUT_KEY] = {day: phase}


def _sale_fits(state, path, ores, sale_rounds, arrival):
    """卖完回炮是否来得及：整趟（去小贩、卖、回炮位、留余量）必须早于敌人到达/天黑。
    夜里只有入夜满 NIGHT_SELL_AFTER 回合后才允许外出卖矿，之前只在后院采矿。"""
    if arrival is None:
        return False
    from .brain import DAY_NIGHT_CYCLE, DAY_ROUNDS, is_day_round
    if not is_day_round(state.round_no):
        elapsed = int(state.round_no or 0) % DAY_NIGHT_CYCLE - DAY_ROUNDS
        if elapsed < NIGHT_SELL_AFTER:
            return False
    return sale_rounds < arrival


def live_pioneer(state):
    return next((r for r in (state.team_our.roles if state.team_our else [])
                 if r.role_type == 'pioneer' and r.health > 0), None)


def pioneer_available_to_buy_voucher(state):
    """进行中的任务、已预约或正在前往任务点的开拓者不去买普通券。"""
    pioneer = live_pioneer(state)
    if not pioneer or state.phase_task:
        return False
    from .pioneer_schedule import has_task_reservation, voucher_is_defense_critical
    if has_task_reservation(state, pioneer) and not voucher_is_defense_critical(state)[0]:
        return False
    return True


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


PRICE_RISE_HOLD_DAYS = 2  # 官方消息说这几天内要涨价的矿，今天先不卖。
HELD_ORE_FULL_RATIO = 0.9  # 背包到这个比例才卖掉囤货中超过半包的部分，避免采不动。


def ores_held_for_price_rise(state):
    """长远规划：官方消息预告接下来几天会涨价的矿，涨价前不卖，等涨价当天再卖。"""
    if state is None:
        return set()
    today_up = price_spike_today(state)
    memory = getattr(state, 'news_memory', None)
    if memory is None:
        try:
            from .world_intel import ores_to_stockpile
            return set(ores_to_stockpile(state)) - today_up
        except Exception:
            return set()
    from .news_memory import game_day
    today = game_day(state.round_no)
    held = set()
    for effect in memory.data.get('oreEffects', []):
        ore = effect.get('affectedOre')
        days = effect.get('priceUpDays') or []
        if ore and any(today < int(day) <= today + PRICE_RISE_HOLD_DAYS for day in days):
            held.add(ore)
    return held - today_up


def _sellable_metal(role, state):
    held = ores_held_for_price_rise(state)
    return [item for item in (role.backpack or []) if item in ('iron', 'copper') and item not in held]


def metal_inventory_value(role, state):
    """背包里现在该卖的铜铁按当前小贩报价计值；无报价计 0，不编造售价。"""
    prices = ore_prices(state)
    return sum(prices.get(name, 0) for name in _sellable_metal(role, state))


def worker_metal_count(role, state=None):
    """背包里现在该卖的铜铁数量；传入 state 时不计为涨价囤着的矿。"""
    return len(_sellable_metal(role, state))


def worker_has_metal(role, state=None):
    return worker_metal_count(role, state) > 0


def team_metal_inventory_value(state):
    return sum(metal_inventory_value(r, state)
               for r in (state.team_our.roles if state.team_our else [])
               if r.role_type == 'worker' and r.health > 0)


def opening_cashout_owner(state):
    """第一门筹资时指定一名持矿工人去小贩，避免两人都空等。"""
    committed = list(state.policy_memory.get('selling_roles') or [])
    holders = [r for r in (state.team_our.roles if state.team_our else [])
               if r.role_type == 'worker' and r.health > 0 and worker_has_metal(r, state)]
    if not holders:
        return None
    for rid in committed:
        owner = next((r for r in holders if r.id == rid), None)
        if owner is not None:
            return owner.id
    holders.sort(key=lambda r: (-worker_metal_count(r, state), -metal_inventory_value(r, state), r.id))
    return holders[0].id


def worker_should_shop_weapon_voucher(role, state, blocked=None):
    """工人买券：本人已持券，或完整代价比较后轮到这名工人。"""
    from .brain import should_upgrade_weapon, weapon_upgrade_due
    if role.role_type != 'worker':
        return False
    if any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in role.backpack):
        return True
    job = state.worker_item_jobs.get(role.id)
    if job and job.get('kind') == 'weapon':
        return True
    if not weapon_upgrade_due(state) and not any(
            item.get('kind') == 'weapon' for item in state.worker_item_jobs.values()):
        return False
    extra = should_upgrade_weapon(state) and any(
        rid != role.id and item.get('kind') == 'weapon'
        for rid, item in state.worker_item_jobs.items())
    buyer = pick_weapon_voucher_buyer(state, blocked, extra=extra)
    return bool(buyer and buyer.id == role.id)


def voucher_funding_gap(state):
    from .brain import weapon_upgrade_due
    if not weapon_upgrade_due(state) and not any(
            job.get('kind') == 'weapon' for job in state.worker_item_jobs.values()):
        return 0
    return max(0, next_weapon_voucher_cost(state) - (state.team_our.gold_num if state.team_our else 0))


def team_voucher_quote_covers(state):
    """已知报价下，全队现金加工人铜铁是否够一张必要券。缺报价时返回 False，不假装够。"""
    gap = voucher_funding_gap(state)
    if gap <= 0:
        return True
    return team_metal_inventory_value(state) >= gap


def _voucher_holder(role):
    return any(isinstance(item, str) and 'WeaponUpgradeVoucher' in item for item in role.backpack)


def _upgrade_target_weapon(state):
    from .brain import WEAPON_TYPES, _pick_upgradeable, _pending_item_job_targets
    job = next((j for j in state.worker_item_jobs.values() if j.get('kind') == 'weapon'), None)
    if job:
        x, y = job['target']
        weapon = next((r for r in state.team_our.roles
                       if r.role_type in WEAPON_TYPES and r.health > 0
                       and r.pos.x == x and r.pos.y == y), None)
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
    from .grid import build_blocked_set
    from .pioneer_schedule import INTERRUPT_RESERVATION_COST, pioneer_task_commitment
    blocked = build_blocked_set(state)
    commitment = pioneer_task_commitment(role, state, blocked)
    if commitment.get('inAcceptRange'):
        return INTERRUPT_RESERVATION_COST * 2
    if commitment.get('reserved') or commitment.get('feasible'):
        return INTERRUPT_RESERVATION_COST
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


def _skip_pioneer_voucher_buyer(role, state, blocked):
    """进行中任务、领取当轮、普通任务预约不把开拓者派去买券；防守必需购买除外。"""
    if role is None or role.role_type != 'pioneer':
        return False
    if state.phase_task:
        return True
    from .pioneer_schedule import pioneer_task_commitment, voucher_is_defense_critical
    critical, _reason = voucher_is_defense_critical(state)
    if critical:
        return False
    commitment = pioneer_task_commitment(role, state, blocked)
    if commitment.get('inAcceptRange') or commitment.get('reserved') or commitment.get('feasible'):
        return True
    return False


def pick_weapon_voucher_buyer(state, blocked=None, extra=False):
    """在能按时完成的人里选综合代价最低的；已持券优先。执行中任务保持稳定，除非阵亡、不可达或赶不上截止。
    extra=True：为首日第二门另找买家，跳过已有武器券任务的人。"""
    if not state.team_our or not state.map_info:
        return None
    from .brain import (
        WEAPON_TYPES, _pending_item_job_targets, _pick_upgradeable, find_zone, item_cost,
        voucher_for, weapon_upgrade_due,
    )
    from .grid import build_blocked_set
    from .opening import MUSTER_BUFFER, mobile_walkable, station_return_steps
    from .tactics import night_wave_cleared, threat_eta_to_base
    if blocked is None:
        blocked = build_blocked_set(state)
    blocked = mobile_walkable(state, blocked, set())
    occupied = {rid for rid, job in state.worker_item_jobs.items() if job.get('kind') == 'weapon'}
    if extra:
        weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=1)
        if weapon is None:
            weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=2)
    else:
        weapon = _upgrade_target_weapon(state)
    name, _ = voucher_for('weapon', (weapon.level or 1) if weapon else 1)
    cost = item_cost(name, state)

    def role_arrival(item):
        if night_wave_cleared(state):
            return None
        return threat_eta_to_base(state, item)

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
        eta = role_arrival(role)
        if eta is not None and total + MUSTER_BUFFER >= eta:
            return False
        return True

    existing = next((j for j in state.worker_item_jobs.values() if j.get('kind') == 'weapon'), None)
    if existing and not extra:
        owner = next((r for r in state.team_our.roles
                      if r.id in state.worker_item_jobs
                      and state.worker_item_jobs[r.id].get('kind') == 'weapon'
                      and r.health > 0), None)
        if still_ok(owner) and not _skip_pioneer_voucher_buyer(owner, state, blocked):
            return owner
    if extra and not weapon_upgrade_due(state):
        return None
    if not weapon_upgrade_due(state) and not existing:
        return None
    if weapon is None:
        return None
    shop = find_zone(state, 'weaponShop')
    best = None
    for role in state.team_our.roles:
        if role.role_type not in ('worker', 'pioneer') or role.health <= 0:
            continue
        if extra and role.id in occupied:
            continue
        if _skip_pioneer_voucher_buyer(role, state, blocked):
            continue
        parts = _voucher_trip_parts(role, state, blocked, weapon, cost)
        if parts is None:
            continue
        time_needed, score = parts
        gun_back = station_return_steps(role, state, blocked, from_pos=weapon.pos)
        if gun_back is None:
            continue
        total = time_needed + gun_back
        eta = role_arrival(role)
        if eta is not None and total + MUSTER_BUFFER >= eta:
            continue
        holder = 0 if _voucher_holder(role) else 1
        shop_dist = chebyshev(role.pos, shop.pos) if shop else 99
        on_shop = 0 if shop_dist == 0 else 1
        role_rank = 0 if role.role_type == 'pioneer' else 1
        key = (holder, on_shop, score + gun_back, total, role_rank, role.id)
        if best is None or key < best[0]:
            best = (key, role, total, score)
    if best is None:
        trace(state, None, 'voucher_no_buyer', '没有人能在截止前完成买券用券并回炮',
              required_gold=cost, available_gold=state.team_our.gold_num if state.team_our else 0,
              threat_eta=threat_eta_to_base(state), weapon_id=None if weapon is None else weapon.id)
        return None
    _, role, time_needed, score = best
    trace(state, role.id, 'voucher_buyer_pick', '按卖矿绕路、到店、使用和回炮的完整代价派人买券',
          time_needed=time_needed, score=score, weapon_id=weapon.id, required_gold=cost, extra=extra)
    return role


def defense_occupancy(role, state, blocked):
    """把回防占用分成三类：returning / must_hold / free。
    只有正在回防移动或必须留守/操炮能挡住普通经济任务；已到家且无强制留守不算永久驻守。"""
    from .brain import WEAPON_TYPES, is_day_round
    from .pioneer_schedule import defense_snapshot
    snap = defense_snapshot(role, state, blocked)
    at_assigned = bool(snap.get('alreadyAtPost'))
    at_gun = at_assigned or any(
        r.health > 0 and r.role_type in WEAPON_TYPES and chebyshev(role.pos, r.pos) <= 1
        for r in (state.team_our.roles if state.team_our else [])
    )
    reasons = list(snap.get('defenseDueReasons') or [])
    if snap.get('pressure') or snap.get('imminentContact'):
        kind = 'must_hold' if at_gun else 'returning'
    elif not is_day_round(state.round_no) and snap.get('defenseSlack', 0) <= 0 and not snap.get('nightWaveCleared'):
        kind = 'must_hold' if at_gun else 'returning'
    elif not snap.get('defenseDue'):
        kind = 'free'
    elif at_gun:
        travel = snap.get('travel')
        eta_lock = 'travel_plus_buffer_vs_eta' in reasons and travel == 0
        kind = 'must_hold' if eta_lock else 'free'
    else:
        kind = 'returning'
    snap = dict(snap)
    snap['occupancy'] = kind
    snap['atGun'] = at_gun
    return kind, snap


def defense_due(role, state, blocked):
    """安全余量不足则回防。与任务候选共用 station_return_detail 路径和到位规则。"""
    from .pioneer_schedule import defense_snapshot
    return defense_snapshot(role, state, blocked)['defenseDue']


def solver_ready_to_submit(state) -> bool:
    from .pioneer_schedule import scheduler_task_session
    session = scheduler_task_session(state)
    if not session:
        return False
    if session.get('stage') in ('submit', 'wait_submit'):
        return True
    return bool(session.get('answer'))


def pioneer_should_hold_task(pioneer, state) -> bool:
    """任务开始后离开任务点就直接失败：有进行中的任务就一直留在任务点，直到做完或超时。"""
    return bool(pioneer is not None and pioneer.health > 0 and state.phase_task)


def muster_for_night(role, state, blocked, reserved):
    """正在回防或必须要塞时才接管；已到岗且无强制留守返回 False，让上层继续评估买券等事务。"""
    from .opening import assign_weapons, station_path, move_on_path
    from .tactics import night_wave_cleared, pressure
    if night_wave_cleared(state):
        return False, None
    if role.id in (getattr(state, 'night_released_ids', None) or ()):
        return False, None  # 夜间已放出采矿的工人，回防时机由 plan_night 按敌人距离单独把关。
    if (state.round_no or 0) < 70 and role.role_type == 'worker' and not pressure(state):
        return False, None  # 首日由 opening FSM 按个人回防截止点调度。
    occupancy, snap = defense_occupancy(role, state, blocked)
    if occupancy == 'free':
        if snap.get('alreadyAtPost') or snap.get('atGun'):
            trace(state, role.id, 'at_post_no_mandatory_hold',
                  '已到炮位/家里，但当前没有强制留守需求，继续评估经济动作',
                  occupancy=occupancy, defenseDue=snap.get('defenseDue'),
                  defenseDueReasons=snap.get('defenseDueReasons'),
                  travel=snap.get('travel'), threatEta=snap.get('threatEta'),
                  travelReason=snap.get('travelReason'))
        return False, None
    if role.role_type == 'pioneer' and pioneer_should_hold_task(role, state):
        return False, None
    weapon = assign_weapons(state).get(role.id)
    from .opening import defense_rounds_remaining
    remaining = defense_rounds_remaining(state, role)
    if weapon is None:
        from .brain import own_station
        from .opening import adjacent_path
        base = own_station(state)
        path = adjacent_path(role, base.pos, blocked | reserved, state) if base else None
        trace(state, role.id, 'no_free_weapon', '进入回防时段但缺少独立武器，先返回基地',
              occupancy=occupancy, threat_eta=snap.get('threatEta'))
        return True, move_on_path(state, role, path, reserved, '没有武器也不留在外面，返回基地')
    path = station_path(role, weapon, blocked | reserved, state)
    trace(state, role.id, 'income_muster',
          '强制留守操炮' if occupancy == 'must_hold' else '安全余量不足，提前回到分配武器',
          occupancy=occupancy, weapon_id=weapon.id,
          remaining_day_rounds=remaining, threat_eta=snap.get('threatEta'),
          defenseDueReasons=snap.get('defenseDueReasons'),
          return_steps=None if path is None else len(path))
    if occupancy == 'must_hold' and snap.get('atGun') and not path:
        return True, None
    return True, move_on_path(state, role, path, reserved,
                              '原地守炮' if occupancy == 'must_hold' and snap.get('atGun') else '停止采矿和购物，提前回防')


def ore_prices(state):
    # 无报价时只按数量触发，不假装知道成交价格。
    return {i.name: max(0, i.price) for i in state.vendor_shop_list if i.name in ('stone', 'iron', 'copper')}


def dusk_cashout_lead(state):
    """第三晚前多留几回合清包买券；其它白天入夜前也要来得及变现。"""
    return PRE_NIGHT3_CASHOUT_LEAD if (state.round_no or 0) // 130 >= 2 else PRE_NIGHT_CASHOUT_LEAD


def _vendor_choices(role, state, blocked, reserved):
    from .opening import adjacent_path
    if not state.map_info:
        return []
    choices = []
    from .brain import is_day_round
    from .opening import night_safe_path
    for vendor in (z for z in state.map_info.zones if z.neutral_type == 'vendor'):
        if is_day_round(state.round_no):
            path = adjacent_path(role, vendor.pos, blocked | reserved, state)
        else:
            path = night_safe_path(role, vendor.pos, blocked | reserved, state)
        if path is not None:
            choices.append((len(path), vendor.pos.x, vendor.pos.y, vendor, path))
    return choices


def _sale_return_rounds(role, state, blocked, ores, path):
    """卖完再回炮的回合数；找不到回路返回 return_time=10000。"""
    from .opening import MUSTER_BUFFER, adjacent_path
    from .tactics import night_wave_cleared, threat_eta_to_base
    sell_actions = max(1, len(ores)) if ores else 1
    selling_pos = path[-1] if path else role.pos
    sale_move = len(path) if path else 0
    weapons = [r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
    proxy = Role(-1, selling_pos, 'worker', 1)
    obstacles = blocked - {(r.pos.x, r.pos.y) for r in state.team_our.roles
                           if r.role_type in ('worker', 'pioneer')}
    return_lengths = [len(p) for p in (adjacent_path(proxy, w.pos, obstacles, state) for w in weapons) if p is not None]
    if return_lengths:
        return_time = min(return_lengths)
    elif not weapons:
        return_time = 0
    else:
        return_time = 10000
    sale_rounds = sale_move + sell_actions + return_time + MUSTER_BUFFER
    arrival = None if night_wave_cleared(state) else threat_eta_to_base(state)
    if arrival is None and not night_wave_cleared(state):
        from .brain import DAY_NIGHT_CYCLE, is_day_round
        from .tactics import threat_robots
        if (not is_day_round(state.round_no) and not threat_robots(state)
                and state.policy_memory.get('night_saw_threat')):
            # 本夜已见过敌人、当前视野里一个不剩：按距天亮的回合数估算，允许夜间外出的工人卖矿。
            arrival = DAY_NIGHT_CYCLE - int(state.round_no) % DAY_NIGHT_CYCLE
    return sale_rounds, return_time, arrival


def in_pre_night_cashout_window(role, state, blocked, reserved=None):
    """入夜前这一段：卖掉还能回防，但再采一趟或拖到截止就会把收益留在背包里。"""
    from .brain import is_day_round
    from .tactics import night_wave_cleared, threat_eta_to_base
    reserved = reserved or set()
    if not is_day_round(state.round_no) or (state.round_no or 0) < 70:
        return False
    if night_wave_cleared(state) or not state.map_info:
        return False
    arrival = threat_eta_to_base(state)
    if arrival is None:
        return False
    ores = sellable_ores(role, state, dump_extra_stone=True)
    probe = ores if ores else Counter({'copper': 1})
    choices = _vendor_choices(role, state, blocked, reserved)
    if not choices:
        return False
    _, _, _, _, path = min(choices, key=lambda c: c[:3])
    sale_rounds, return_time, _ = _sale_return_rounds(role, state, blocked, probe, path)
    if return_time >= 10000 or not _sale_fits(state, path, probe, sale_rounds, arrival):
        return False
    return arrival - sale_rounds <= dusk_cashout_lead(state)


def sellable_ores(role, state, dump_extra_stone=False, ignore_stockpile=False):
    from .brain import own_station
    from .opening import staged_wall_plan
    ores = Counter(i for i in role.backpack if i in ('stone', 'iron', 'copper'))
    try:
        from .news_memory import game_day
        memory = getattr(state, "news_memory", None)
        if memory is not None:
            day = game_day(state.round_no)
            stockpile = set(memory.ores_to_stockpile(day))
            price_up = set(memory.price_boosted_ores(day))
        else:
            from .world_intel import ores_to_stockpile, ores_in_spike
            stockpile = set(ores_to_stockpile(state))
            price_up = set(ores_in_spike(state))
    except Exception:
        stockpile, price_up = set(), set()
    if ignore_stockpile:
        stockpile = set()
    cap = role.back_pack_capability or 0
    nearly_full = bool(cap) and len(role.backpack or []) >= cap * HELD_ORE_FULL_RATIO
    held = ores_held_for_price_rise(state)
    for ore in held:
        # 涨价前一律不卖；只有背包快满、采不动了，才卖掉超过半包的部分。
        ores[ore] = max(0, ores[ore] - cap // 2) if nearly_full else 0
    stockpile_cap = cap // 2 if cap else None
    for ore in stockpile - price_up - held:
        if stockpile_cap is None:
            ores[ore] = 0
        else:
            ores[ore] = max(0, ores[ore] - stockpile_cap)
    base = own_station(state)
    reserve = 0
    if base and role.role_type == 'worker':
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
        missing = len(set(staged_wall_plan(state, base)) - walls)
        workers = [r for r in state.team_our.roles if r.role_type == 'worker' and r.health > 0]
        hands = max(1, len(workers))
        from .brain import max_health
        from .opening import critical_wall_missing
        if dump_extra_stone and not critical_wall_missing(state):
            # 入夜前正面已封，石头也卖掉换成金币；迎敌缺口仍留石给紧急封堵。
            reserve = 0
        elif (state.round_no or 0) >= 70 and missing:
            # 第一晚后建墙用石全部留着，只卖超出缺口的部分。
            others = sum(r.backpack.count('stone') for r in workers if r.id != role.id)
            still_need = max(0, missing - others)
            reserve = min(ores['stone'], still_need)
        else:
            reserve = min(BUILD_STONE_RESERVE, (missing + hands - 1) // hands)
        if base.health < max_health(base) * 0.7:
            reserve = min(reserve, 1)
    ores['stone'] = max(0, ores['stone'] - reserve)
    return +ores


def liquidate(role, state, blocked, reserved, force_reason=None, keep_wall_stone=False):
    """返回(是否接管, 指令)。往返时间不足时停止外出，转入原有防守流程。
    force_reason 表示强制清包：连同囤货一起卖；keep_wall_stone=False 时多余石头也卖掉。"""
    from .brain import max_health
    from .opening import move_on_path
    from .tactics import night_wave_cleared
    committed = state.policy_memory.setdefault('selling_roles', [])
    ores = sellable_ores(role, state, dump_extra_stone=bool(force_reason) and not keep_wall_stone,
                         ignore_stockpile=bool(force_reason))
    vendors = [z for z in state.map_info.zones if z.neutral_type == 'vendor'] if state.map_info else []
    adjacent = any(chebyshev(role.pos, z.pos) <= 1 for z in vendors)
    choices = _vendor_choices(role, state, blocked, reserved)
    path = []
    sale_rounds = 0
    arrival = None
    return_time = 0
    in_window = False
    if choices:
        _, _, _, _, path = min(choices, key=lambda c: c[:3])
        probe = ores if ores else Counter({'copper': 1})
        sale_rounds, return_time, arrival = _sale_return_rounds(role, state, blocked, probe, path)
        in_window = (
            (state.round_no or 0) >= 70
            and arrival is not None
            and return_time < 10000
            and _sale_fits(state, path, probe, sale_rounds, arrival)
            and arrival - sale_rounds <= dusk_cashout_lead(state)
        )
        if in_window and not force_reason:
            dumped = sellable_ores(role, state, dump_extra_stone=True)
            if dumped:
                ores = dumped
                sale_rounds, return_time, arrival = _sale_return_rounds(role, state, blocked, ores, path)
    if not ores:
        if role.id in committed:
            committed.remove(role.id)
        return False, None
    prices = ore_prices(state)
    quoted_value = sum(prices.get(name, 0) * count for name, count in ores.items())
    value_unknown = any(item in ('iron', 'copper') and prices.get(item, 0) <= 0
                        for item in ores)
    value = quoted_value
    triggers = [force_reason] if force_reason else []
    from .brain import should_upgrade_weapon
    from .opening import OPENING_METAL_BATCH, day_rounds_remaining, live_l2_weapon_count, REQUIRED_OPENING_UPGRADES, survival_walls_locked
    try:
        from .news_memory import game_day
        memory = getattr(state, "news_memory", None)
        if memory is not None:
            news_sell_ores = set(ores) & set(memory.price_boosted_ores(game_day(state.round_no)))
        else:
            from .world_intel import ores_in_spike
            news_sell_ores = set(ores) & set(ores_in_spike(state))
    except Exception:
        news_sell_ores = set()
    if news_sell_ores:
        triggers.append('官方消息显示该矿今日涨价，优先卖出囤货')
    waiting_weapon_job = any(job.get('kind') == 'weapon' for job in state.worker_item_jobs.values())
    need_voucher = should_upgrade_weapon(state) or waiting_weapon_job
    gap = voucher_funding_gap(state)
    cap = role.back_pack_capability or 0
    fill = (len(role.backpack) / cap) if cap else 1.0
    metal_count = worker_metal_count(role, state)
    first_upgrade_open = live_l2_weapon_count(state) < REQUIRED_OPENING_UPGRADES
    team_covers = team_voucher_quote_covers(state)
    cashout_id = opening_cashout_owner(state)
    designated = cashout_id == role.id or role.id in committed
    holders = sum(1 for r in (state.team_our.roles if state.team_our else [])
                  if r.role_type == 'worker' and r.health > 0 and worker_has_metal(r, state))
    near_cutoff = day_rounds_remaining(state.round_no) <= PRE_NIGHT_CASHOUT_LEAD
    pack_full = fill >= 1.0 or (cap and len(role.backpack) >= cap)
    if survival_walls_locked(state) and (state.round_no or 0) < 70 and not force_reason:
        pending = (state.policy_memory or {}).get('cashout_pending')
        clear_pack = pack_full and metal_count and role.backpack.count('stone') == 0
        if not pending and not clear_pack:
            return False, None
    if role.role_type == 'worker' and worker_should_shop_weapon_voucher(role, state) and gap and (
            quoted_value >= gap or (value_unknown and metal_count)):
        triggers.append('卖掉本包后工人去买武器升级券')
    if (state.round_no or 0) < 70:
        if first_upgrade_open and metal_count:
            if team_covers:
                triggers.append('全队现金加已知矿物估值已够本次必要升级券')
            if pack_full:
                triggers.append('背包已满，主动变现铜铁')
            if metal_count >= OPENING_METAL_BATCH:
                triggers.append('铜铁达到批量阈值，前往小贩')
            if near_cutoff:
                triggers.append('接近白天截止，先卖掉铜铁')
            if value_unknown:
                triggers.append('小贩暂无报价，仍出售铜铁等待快照金币')
            if designated and holders >= 2:
                triggers.append('两名工人都持有铜铁，指定一人汇总变现')
            if designated and pack_full:
                if '持矿工人无法继续有效采矿，先去变现' not in triggers:
                    triggers.append('持矿工人无法继续有效采矿，先去变现')
            if not triggers and role.id not in committed:
                return False, None
        elif need_voucher and gap and state.team_our.gold_num + quoted_value >= next_weapon_voucher_cost(state):
            triggers.append('现金加本包估值已够本次必要升级券')
        elif not triggers and role.id not in committed:
            return False, None
    else:
        if gap and value >= gap:
            triggers.append('卖掉本包即可完成必要武器升级')
        if role.health < max_health(role) * 0.6:
            triggers.append('低血量携矿风险')
        if fill >= BATCH_FILL_RATIO:
            triggers.append('背包过半，批量变现')
        if cap and cap - len(role.backpack) <= NEAR_CAP_SLOTS:
            triggers.append('背包即将满载，批量变现')
        if in_window:
            triggers.append('入夜前清空背包，转化收益升级武器')
    if not choices:
        if not (triggers or adjacent or role.id in committed):
            return False, None
        trace(state, role.id, 'sale_unreachable', '需要变现，但当前没有可达的小贩；不继续盲目采矿', ore_value=value)
        return True, None
    if return_time >= 10000:
        trace(state, role.id, 'sale_too_late', '卖完后找不到回路，不批准这趟外出',
              estimated_rounds=sale_rounds, return_time=return_time)
        return False, None
    if not night_wave_cleared(state):
        if not _sale_fits(state, path, ores, sale_rounds, arrival):
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
    if role.id not in committed:
        committed.append(role.id)
    if value_unknown:
        trace(state, role.id, 'sale_value_unknown',
              '小贩暂无铜铁报价，仍执行出售并等待服务器快照金币',
              sellable=dict(ores), quoted_value=quoted_value, known_prices=prices)
    trace(state, role.id, 'cashout_priority', '急用立即变现，入夜前清空背包换成火力', triggers=triggers,
          sellable=dict(ores), quoted_value=quoted_value, sale_value_unknown=value_unknown,
          known_prices=prices,
          stone_reserved=role.backpack.count('stone')-ores.get('stone', 0),
          trip_rounds=sale_rounds, threat_eta=arrival, fill_ratio=round(fill, 2))
    if not path:
        name = max(ores, key=lambda n: (ores[n] * prices.get(n, 0), ores[n], n))
        return True, selected(state, role.id, {'action': 'sell', 'name': name, 'num': ores[name]}, '批量出售同种矿石，减少往返')
    return True, move_on_path(state, role, path, reserved, '本趟批量变现，不采一点卖一点')


def claimed_mines(state, exclude_role_id=None):
    claimed = set()
    for rid, target in (state.policy_memory.get(MINE_TARGETS_KEY) or {}).items():
        if exclude_role_id is not None and str(rid) == str(exclude_role_id):
            continue
        if not isinstance(target, dict):
            continue
        try:
            claimed.add((int(target['x']), int(target['y'])))
        except (KeyError, TypeError, ValueError):
            continue
    return claimed


def get_mine_target(state, role_id):
    target = (state.policy_memory.get(MINE_TARGETS_KEY) or {}).get(str(role_id))
    return target if isinstance(target, dict) else None


def set_mine_target(state, role_id, mine):
    mem = dict(state.policy_memory.get(MINE_TARGETS_KEY) or {})
    mem[str(role_id)] = {'x': mine.pos.x, 'y': mine.pos.y, 'ore': mine.neutral_type}
    state.policy_memory[MINE_TARGETS_KEY] = mem


def clear_mine_target(state, role_id):
    mem = dict(state.policy_memory.get(MINE_TARGETS_KEY) or {})
    if str(role_id) not in mem:
        return
    mem.pop(str(role_id), None)
    if mem:
        state.policy_memory[MINE_TARGETS_KEY] = mem
    else:
        state.policy_memory.pop(MINE_TARGETS_KEY, None)


def trip_collect_limit(role, state, path_len=0, return_len=0, purpose='income'):
    """本趟还能采几下：背包空位、单趟上限、入夜/清包前剩余工时。"""
    slots = max(0, (role.back_pack_capability or 1) - len(role.backpack))
    cap = min(slots, MINE_TRIP_CAP)
    if cap <= 0:
        return 0
    from .brain import is_day_round
    from .opening import MUSTER_BUFFER
    from .tactics import night_wave_cleared, threat_eta_to_base
    if night_wave_cleared(state):
        return cap
    if not is_day_round(state.round_no):
        return cap
    arrival = threat_eta_to_base(state, role)
    if arrival is None:
        return cap
    lead = dusk_cashout_lead(state) if purpose == 'income' else 0
    slack = arrival - lead - MUSTER_BUFFER - path_len - return_len
    return max(0, min(cap, slack))


def voucher_collect_plan(role, state, blocked, reserved, mine, remaining_value, prices=None, path=None):
    """按 vendorShopList 报价，估算采这座矿凑够升级券缺口的回合：去程 + 采集 + 去小贩。
    没有报价或矿不可达时返回 None，不编造价格。"""
    from .opening import adjacent_path
    prices = ore_prices(state) if prices is None else prices
    price = prices.get(mine.neutral_type, 0)
    if price <= 0 or remaining_value <= 0:
        return None
    if path is None:
        path = adjacent_path(role, mine.pos, blocked | reserved, state)
    if path is None:
        return None
    units = -(-int(remaining_value) // price)
    slots = max(0, (role.back_pack_capability or 0) - len(role.backpack))
    vendor_len = vendor_return_steps(mine, state, blocked, reserved)
    return {
        'path': path,
        'units': units,
        'price': price,
        'path_len': len(path),
        'vendor_len': vendor_len,
        'rounds': len(path) + units + vendor_len,
        'fits_backpack': units <= slots,
    }


def voucher_ore_remaining_value(role, state):
    """当前金币加全队工人铜铁已知估值后，买一张武器升级券还差多少。无报价不计收益。"""
    gold = state.team_our.gold_num if state.team_our else 0
    return max(0, next_weapon_voucher_cost(state) - gold - team_metal_inventory_value(state))


def vendor_return_steps(mine, state, blocked, reserved):
    """从矿点走到小贩邻格的寻路长度；没有小贩时采石建墙不依赖回程。"""
    from .opening import adjacent_path
    if state.map_info is None:
        return 0
    vendors = [z for z in state.map_info.zones if z.neutral_type == 'vendor']
    if not vendors:
        return 0
    proxy = Role(id=-1, pos=mine.pos, role_type='worker', health=1)
    best = None
    for vendor in vendors:
        path = adjacent_path(proxy, vendor.pos, blocked | reserved, state)
        if path is None:
            continue
        n = len(path)
        if best is None or n < best:
            best = n
    if best is None:
        return (state.map_info.width or 0) + (state.map_info.height or 0)
    return best


def _mine_at(state, x, y, want_ores):
    if state.map_info is None:
        return None
    for mine in state.map_info.zones:
        if mine.pos.x == x and mine.pos.y == y and mine.neutral_type in want_ores:
            return mine
    return None


def ore_soft_capped(role, ore, ratio=0.8):
    cap = role.back_pack_capability or 0
    if cap <= 0:
        return False
    return (role.backpack or []).count(ore) >= int(cap * ratio)


def pick_mine(role, state, blocked, reserved, want_ores, purpose='income'):
    """一人一矿：能沿用粘性目标就继续；筹资买券时按 vendorShopList 选总回合最短的铜铁。
    夜里只采防线后方、离机器人远的矿，并按离基地最近选，避免绕路或挨打。"""
    from .brain import is_day_round, own_station
    from .opening import adjacent_path, night_danger_cells, night_strict_path
    want = set(want_ores)
    if not want or state.map_info is None:
        return None
    night = not is_day_round(state.round_no)
    danger = night_danger_cells(state) if night else set()
    base = own_station(state) if (night or purpose == 'stone') else None

    def route_to(mine):
        if night:
            if (mine.pos.x, mine.pos.y) in danger:
                return None
            # 采矿不是回防等紧急移动，夜里不允许寻路降级后穿越正面或机器人
            # 危险区；没有严格安全路线就留守。
            return night_strict_path(role, mine.pos, blocked | reserved, state)
        return adjacent_path(role, mine.pos, blocked | reserved, state)

    def home_distance(mine):
        return chebyshev(mine.pos, base.pos) if base is not None else 0
    prices = ore_prices(state)
    occupied = claimed_mines(state, exclude_role_id=role.id)
    remaining_value = voucher_ore_remaining_value(role, state) if purpose == 'voucher' else 0
    if purpose == 'voucher' and remaining_value <= 0:
        return None
    if purpose in ('income', 'voucher'):
        uncapped = {ore for ore in want if not ore_soft_capped(role, ore)}
        if uncapped:
            capped = want - uncapped
            if capped:
                clear_mine_target(state, role.id)
                trace(state, role.id, 'ore_soft_cap_rotate',
                      '单种矿石已达到背包80%，改采其它矿种避免背包单一化',
                      capped=sorted(capped), remaining=sorted(uncapped),
                      backpack_count={ore: (role.backpack or []).count(ore) for ore in sorted(want)})
            want = uncapped

    def score_mine(mine, path):
        if purpose == 'voucher':
            plan = voucher_collect_plan(
                role, state, blocked, reserved, mine, remaining_value, prices=prices, path=path,
            )
            if plan is None:
                return None
            if not plan['fits_backpack']:
                return None
            return -plan['rounds'], plan['units'], plan['path_len'], plan['vendor_len'], plan
        path_len = len(path)
        return_len = vendor_return_steps(mine, state, blocked, reserved) if purpose == 'income' else 0
        batch = trip_collect_limit(role, state, path_len=path_len, return_len=return_len, purpose=purpose)
        if batch <= 0:
            return None
        score = batch * prices.get(mine.neutral_type, 1) / (path_len + batch + return_len + 1)
        return score, batch, path_len, return_len, None

    sticky = get_mine_target(state, role.id)
    if sticky and purpose == 'stone' and base is not None:
        from .opening import attack_side_of_front
        try:
            if attack_side_of_front(state, base, Pos(int(sticky['x']), int(sticky['y']))):
                sticky = None
                clear_mine_target(state, role.id)
        except (KeyError, TypeError, ValueError):
            pass
    if sticky:
        try:
            mine = _mine_at(state, int(sticky['x']), int(sticky['y']), want)
        except (KeyError, TypeError, ValueError):
            mine = None
        if mine is not None:
            path = route_to(mine)
            ranked = None if path is None else score_mine(mine, path)
            if ranked is not None:
                score, batch, path_len, return_len, _plan = ranked
                set_mine_target(state, role.id, mine)
                trace(state, role.id, 'sticky_mine', '沿用尚未采完的矿点', mineral=mine.neutral_type,
                      batch=batch, path_len=path_len, return_len=return_len, score=round(score, 4))
                return mine, path

    candidates = []
    for mine in state.map_info.zones:
        if mine.neutral_type not in want:
            continue
        path = route_to(mine)
        if path is None:
            continue
        ranked = score_mine(mine, path)
        if ranked is None:
            continue
        score, batch, path_len, return_len, plan = ranked
        claimed = (mine.pos.x, mine.pos.y) in occupied
        if night or purpose == 'stone':
            # 夜里、以及施工工采石：只看往返距离；迎敌墙外侧的矿大幅降权，避免封在墙外绕路。
            from .opening import attack_side_of_front
            attack_penalty = 0
            if purpose == 'stone' and base is not None and attack_side_of_front(state, base, mine.pos):
                attack_penalty = 1000
            candidates.append((attack_penalty + (1 if claimed else 0), path_len + home_distance(mine), path_len,
                               mine, path, batch, return_len, score))
        elif purpose == 'voucher':
            candidates.append((plan['rounds'], 1 if claimed else 0, path_len, mine, path, batch, return_len, score))
        else:
            candidates.append((1 if claimed else 0, -score, path_len, mine, path, batch, return_len, score))
    if not candidates:
        trace(state, role.id, 'no_reachable_mine', '当前没有可达矿点')
        return None
    chosen = min(candidates, key=lambda c: c[:3])
    primary, secondary, path_len, mine, path, batch, return_len, score = chosen
    set_mine_target(state, role.id, mine)
    price = prices.get(mine.neutral_type)
    if purpose == 'voucher':
        trace(state, role.id, 'voucher_mine', '按小贩报价选凑够升级券总回合最短的矿',
              mineral=mine.neutral_type, price=price, collect_units=batch,
              path_len=path_len, vendor_len=return_len, rounds=primary, claimed=bool(secondary),
              remaining_value=remaining_value, vendor_prices=prices)
    else:
        trace(state, role.id, 'income_mine', '按本趟可采数量、报价与寻路成本估算矿点收益',
              mineral=mine.neutral_type, price=price, batch=batch,
              path_len=path_len, return_len=return_len, claimed=bool(primary), score=round(score, 4),
              estimate_note='未知报价按等权比较；回程是到小贩的寻路长度')
    return mine, path


def go_mine(role, state, blocked, reserved, want_ores, purpose='income',
            travel_reason='', collect_reason=''):
    from .opening import move_on_path
    picked = pick_mine(role, state, blocked, reserved, want_ores, purpose=purpose)
    if picked is None:
        clear_mine_target(state, role.id)
        return None
    mine, path = picked
    if path:
        if travel_reason:
            reason = travel_reason
        elif purpose == 'voucher':
            reason = '按小贩报价前往凑够升级券总回合最短的矿'
        else:
            reason = '前往本趟批量收益较高的可达矿点'
        return move_on_path(state, role, path, reserved, reason)
    reason = collect_reason or ('采集铜铁，凑够升级券' if purpose == 'voucher' else '采集矿石，凑够一趟再出售')
    return selected(state, role.id, {'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}]}, reason)


def profitable_mine(role, state, blocked, reserved):
    """按本趟真正采得完的数量估算收益，并粘住已占矿点。仅工人可 collect。"""
    from .opening import stones_cover_wall_plan
    if role.role_type != 'worker':
        trace(state, role.id, 'pioneer_cannot_collect', '采集仅工人可用，开拓者不采矿、不建墙')
        return None
    if in_pre_night_cashout_window(role, state, blocked, reserved):
        clear_mine_target(state, role.id)
        trace(state, role.id, 'cashout_skip_mine', '入夜前停止采矿，把背包收益换成金币和火力')
        return None
    if len(role.backpack) >= role.back_pack_capability:
        clear_mine_target(state, role.id)
        trace(state, role.id, 'backpack_full', '背包已满，停止采矿')
        return None
    skip_stone = stones_cover_wall_plan(state)
    want = {'iron', 'copper'}
    try:
        from .news_memory import game_day
        memory = getattr(state, "news_memory", None)
        if memory is not None:
            day = game_day(state.round_no)
            stockpile = set(memory.ores_to_stockpile(day))
            banned = set(memory.banned_ores(day))
        else:
            from .world_intel import ore_blocked, ores_to_stockpile
            stockpile = set(ores_to_stockpile(state))
            banned = {ore for ore in ('iron', 'copper', 'stone') if ore_blocked(state, ore)}
    except Exception:
        stockpile, banned = set(), set()
    if not skip_stone:
        want.add('stone')
    want -= banned
    priority = (stockpile & {'iron', 'copper', 'stone'}) - banned
    if priority:
        want = priority
        trace(state, role.id, 'news_stockpile_mine',
              '官方消息预告后续禁采/涨价，今天优先抢收对应矿石',
              ores=sorted(priority), banned=sorted(banned))
    return go_mine(role, state, blocked, reserved, want_ores=want, purpose='income')
