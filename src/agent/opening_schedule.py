"""第一天工人唯一调度：跨回合持久的五阶段状态机。其它函数只执行原子动作。"""
from .protocol import Pos
from .grid import build_blocked_set, chebyshev
from .decision_log import trace, selected

STAGE_BUILD_WEAPONS = 'BUILD_WEAPONS'
STAGE_FUND = 'FUND_FIRST_UPGRADE'
STAGE_APPLY = 'APPLY_FIRST_UPGRADE'
STAGE_WALL = 'BUILD_SURVIVAL_WALL'
STAGE_MUSTER = 'MUSTER'
OPENING_STAGES = (STAGE_BUILD_WEAPONS, STAGE_FUND, STAGE_APPLY, STAGE_WALL, STAGE_MUSTER)
LEGAL_TRANSITIONS = {
    STAGE_BUILD_WEAPONS: {STAGE_FUND, STAGE_WALL, STAGE_MUSTER},
    STAGE_FUND: {STAGE_APPLY, STAGE_WALL, STAGE_MUSTER},
    STAGE_APPLY: {STAGE_WALL, STAGE_MUSTER},
    STAGE_WALL: {STAGE_MUSTER},
    STAGE_MUSTER: set(),
}
# 第一晚之前墙比首升炮更关键；三炮齐后直接修最低生存墙。
FIRST_UPGRADE_CUTOFF = 0
GOAL_SWITCH_PENALTY = 4
GOAL_STALL_ROUNDS = 3
MINE_CLEARLY_CLOSER_STEPS = 2
CLAIMED_MINE_MAX_DETOUR = 3
WORKER_GOALS_KEY = 'opening_worker_goals'
WORKER_ROLES_KEY = 'opening_worker_roles'
CASHOUT_COMMIT_KEY = 'opening_cashout_commit'
ROLE_DUE_KEY = 'opening_role_due'
# 最后一趟卖矿的出发窗口：剩余回合在 [绕路耗时, 绕路耗时 + 该值] 之间才出发，保证卖完还来得及回炮。
CASHOUT_WINDOW = 2
# 回炮时绕开队友的路线比直穿多出这么多步以上，就等队友让开。
MUSTER_DETOUR_WAIT = 3


def opening_cycle(state):
    return (state.round_no or 0) % 130


def past_first_upgrade_cutoff(state, remaining=None):
    from .brain import DAY_ROUNDS
    from .opening import day_rounds_remaining
    if remaining is None:
        remaining = day_rounds_remaining(state.round_no)
    return opening_cycle(state) >= FIRST_UPGRADE_CUTOFF or remaining <= (DAY_ROUNDS - FIRST_UPGRADE_CUTOFF)


def current_opening_stage(state):
    stage = (state.policy_memory or {}).get('opening_stage')
    if stage in OPENING_STAGES:
        return stage
    return None


def _set_opening_stage(state, nxt, reason):
    prev = current_opening_stage(state)
    if prev == nxt:
        return nxt
    if prev and nxt not in LEGAL_TRANSITIONS.get(prev, set()):
        trace(state, None, 'opening_stage_blocked', '拒绝非法阶段回退',
              previous=prev, rejected=nxt, reason=reason)
        return prev
    state.policy_memory['opening_stage'] = nxt
    if nxt == STAGE_WALL:
        state.policy_memory['opening_commit'] = 'survival_walls'
    elif nxt in (STAGE_BUILD_WEAPONS, STAGE_FUND):
        state.policy_memory.pop('opening_commit', None)
    if prev != nxt:
        _clear_goals_for_stage_change(state)
        trace(state, None, 'opening_stage', '开局阶段转换',
              previous=prev, stage=nxt, reason=reason, cycle=opening_cycle(state))
    return nxt


def resolve_opening_stage(state, remaining=None, muster_need=3, gold=None):
    """唯一战略状态源。只做合法前向转换。"""
    from .brain import WEAPON_TYPES, item_cost
    from .opening import MUSTER_BUFFER, day_rounds_remaining, live_l2_weapon_count, opening_has_voucher
    if remaining is None:
        remaining = day_rounds_remaining(state.round_no)
    weapons = [r for r in (state.team_our.roles if state.team_our else [])
               if r.role_type in WEAPON_TYPES and r.health > 0]
    if gold is None:
        gold = state.team_our.gold_num if state.team_our else 0
    cost = item_cost('WeaponUpgradeVoucher1', state)
    has_voucher = opening_has_voucher(state)
    l2 = live_l2_weapon_count(state)
    stage = current_opening_stage(state)
    cutoff = past_first_upgrade_cutoff(state, remaining)

    if remaining <= MUSTER_BUFFER:
        return _set_opening_stage(state, STAGE_MUSTER, 'muster_cutoff')
    if stage == STAGE_MUSTER:
        return STAGE_MUSTER
    if stage == STAGE_WALL:
        return STAGE_WALL
    if stage == STAGE_APPLY:
        if l2 >= 1:
            return _set_opening_stage(state, STAGE_WALL, 'first_upgrade_confirmed')
        return STAGE_APPLY
    day1 = (state.round_no or 0) < 70
    if len(weapons) < 3:
        return _set_opening_stage(state, STAGE_BUILD_WEAPONS, 'need_three_weapons')
    if l2 >= 1:
        return _set_opening_stage(state, STAGE_WALL, 'first_upgrade_confirmed')
    if stage in (None, STAGE_BUILD_WEAPONS):
        if day1:
            return _set_opening_stage(state, STAGE_WALL, 'day1_walls_before_upgrade')
        if cutoff and gold < cost and not has_voucher:
            return _set_opening_stage(state, STAGE_WALL, 'upgrade_cutoff_unfunded')
        return _set_opening_stage(state, STAGE_FUND, 'three_weapons_ready')
    if stage == STAGE_FUND:
        if has_voucher:
            return _set_opening_stage(state, STAGE_APPLY, 'voucher_in_hand')
        if cutoff and gold < cost:
            return _set_opening_stage(state, STAGE_WALL, 'upgrade_cutoff_unfunded')
        return STAGE_FUND
    return _set_opening_stage(state, STAGE_FUND if len(weapons) >= 3 else STAGE_BUILD_WEAPONS, 'recover')


def flags_from_opening_stage(stage, gold, cost, has_voucher, cutoff=False, upgraded_count=0):
    """诊断标志：与 FSM 真实允许的动作对齐（不单独门控 dispatch）。"""
    funded = has_voucher or gold >= cost
    log_phase = {
        STAGE_BUILD_WEAPONS: '武器',
        STAGE_FUND: '筹资升级',
        STAGE_APPLY: 'APPLY_FIRST_UPGRADE',
        STAGE_WALL: 'SURVIVAL_WALL',
        STAGE_MUSTER: '就位',
    }.get(stage, stage)
    mining = stage == STAGE_FUND and not funded and not cutoff
    need_first = upgraded_count < 1
    flags = {
        'opening_phase': log_phase,
        'allow_walls': stage in (STAGE_WALL, STAGE_APPLY) or (stage == STAGE_FUND and cutoff and funded),
        # WALL 会卖闲置铜铁 / 经济工筹资卖矿 / 夜前清包；与 opening_sell_metal 路径一致
        'allow_sell': stage in (STAGE_FUND, STAGE_WALL),
        'allow_mine': mining,
        'allow_income_mine': mining,
        'allow_stone_mine': stage == STAGE_WALL,
        'allow_upgrade': False,
        'allow_first_upgrade': False,
        'upgrade_funded': funded,
        'upgrade_safe': stage == STAGE_FUND and funded and not cutoff,
        'required_done': stage in (STAGE_WALL, STAGE_MUSTER),
        'fallback_reason': 'upgrade_cutoff_unfunded' if stage == STAGE_WALL and not funded else None,
        'funding_reason': (
            'have_voucher' if has_voucher else 'gold_ready' if gold >= cost else 'unfunded'
        ),
    }
    if stage in (STAGE_FUND, STAGE_APPLY) and funded and need_first:
        flags['allow_upgrade'] = True
        flags['allow_first_upgrade'] = True
    if stage == STAGE_WALL:
        # 持券要去用；未升完第一门且有钱可买；已升完不买第二张
        flags['allow_upgrade'] = has_voucher or (need_first and funded)
        flags['allow_first_upgrade'] = flags['allow_upgrade']
    if stage == STAGE_MUSTER:
        flags.update(allow_walls=False, allow_upgrade=False, allow_sell=False,
                     allow_mine=False, allow_income_mine=False, allow_stone_mine=False)
    return flags


def _goals(state):
    return state.policy_memory.setdefault(WORKER_GOALS_KEY, {})


def _goal(state, role_id):
    raw = _goals(state).get(str(role_id))
    return raw if isinstance(raw, dict) else None


def _clear_goals_for_stage_change(state):
    _goals(state).clear()
    from .economy import clear_mine_target
    for role in (state.team_our.roles if state.team_our else []):
        if role.role_type == 'worker':
            clear_mine_target(state, role.id)
    (state.policy_memory.get('opening_wall_targets') or {}).clear()


def opening_worker_roles(state):
    """首日固定分工：一名施工工，一名经济工；成员死亡时自动重选。"""
    workers = sorted(
        (r for r in (state.team_our.roles if state.team_our else [])
         if r.role_type == 'worker' and r.health > 0),
        key=lambda r: r.id,
    )
    existing = state.policy_memory.get(WORKER_ROLES_KEY)
    alive_ids = {w.id for w in workers}
    if isinstance(existing, dict):
        builder = existing.get('builder')
        economist = existing.get('economist')
        if builder in alive_ids and (economist in alive_ids or economist is None):
            return existing
    roles = {'builder': None, 'economist': None}
    if workers:
        roles['builder'] = workers[0].id
    if len(workers) >= 2:
        roles['economist'] = workers[1].id
    state.policy_memory[WORKER_ROLES_KEY] = roles
    trace(state, None, 'opening_worker_roles', '首日工人固定分工：施工工负责武器/墙，经济工负责采卖矿和升级券',
          builder=roles['builder'], economist=roles['economist'])
    return roles


def opening_worker_mode(state, role):
    if role.role_type != 'worker':
        return None
    roles = opening_worker_roles(state)
    if role.id == roles.get('builder'):
        return 'builder'
    if role.id == roles.get('economist'):
        return 'economist'
    return 'backup'


def economist_should_help_wall(state, remaining, role):
    """经济工只在有限、可完成的条件下接管墙，完成后自动回到升级闭环，不会变成长期石工。"""
    if role.role_type != 'worker':
        return False
    from .opening import MUSTER_BUFFER, survival_wall_missing
    from . import work_orders as wo
    roles = opening_worker_roles(state)
    builder = next((r for r in (state.team_our.roles if state.team_our else [])
                    if r.id == roles.get('builder') and r.health > 0), None)
    if builder is None or builder.id == role.id:
        return True  # 建造工阵亡或不可用：工作单交接，墙任务不能消失。
    blocked = build_blocked_set(state)
    orders = wo.compute_work_orders(state, blocked)
    if orders['emergency_defense']['active']:
        return True
    missing = survival_wall_missing(state)
    if not missing:
        return False
    # 经济工已经拿着石头，直接补关键缺口比跑回去采铜铁划算。
    if wo.stone_count(role) > 0:
        return True
    program = orders['wall_program']
    # 只看最低墙线的余量：普通扩墙没修完不是把经济工变成石工的理由。
    slack = program.get('minimum_slack')
    if slack is not None and slack < MUSTER_BUFFER:
        trace(state, role.id, 'economist_helps_wall', '最低墙线时间余量不足，经济工临时接管一个批次',
              minimum_slack=slack, minimum_finish_eta=program.get('minimum_finish_eta'),
              wall_deadline=program.get('wall_deadline'))
        return True
    return False


def _store_goal(state, role, stage, kind, target_type, target_pos, distance, stalled, switch_reason,
                last_pos=None, prev_pos=None, oscillation_detected=False):
    pos = [int(target_pos[0]), int(target_pos[1])] if target_pos is not None else None
    _goals(state)[str(role.id)] = {
        'stage': stage,
        'kind': kind,
        'target_type': target_type,
        'target_pos': pos,
        'assigned_round': state.round_no,
        'last_distance': distance,
        'stalled_rounds': stalled,
        'switch_reason': switch_reason,
        'last_pos': [role.pos.x, role.pos.y],
        'prev_pos': last_pos,
        'oscillation_detected': bool(oscillation_detected),
    }


def _path_target(path, fallback):
    if path:
        return path[-1].x, path[-1].y
    return fallback


def opening_move(state, role, path, reserved, target_pos, reason, goal_kind, goal_type, switch_reason, stage):
    from .opening import move_on_path
    dist_before = 0 if path is None else len(path)
    here = (role.pos.x, role.pos.y)
    goal = _goal(state, role.id)
    prev = tuple(goal['last_pos']) if goal and goal.get('last_pos') else None
    oscillating = False
    if path:
        nxt = (path[0].x, path[0].y)
        if prev and nxt == prev and here != prev:
            oscillating = True
            trace(state, role.id, 'oscillation_detected', '检测到 A→B→A，保持原目标走当前 BFS 下一步',
                  oscillation_detected=True, current_position={'x': here[0], 'y': here[1]},
                  next_position={'x': nxt[0], 'y': nxt[1]}, target_position=target_pos)
        same_goal = bool(goal and goal.get('target_pos') == (list(target_pos) if target_pos else None))
        # 距离只记实际观测：上回合同一目标时的路径长度 vs 本回合路径长度，不预估移动后的距离。
        prev_distance = goal.get('last_distance') if same_goal else None
        trace(state, role.id, 'opening_step', '向目标前进一步',
              current_position={'x': here[0], 'y': here[1]},
              next_position={'x': nxt[0], 'y': nxt[1]},
              target_position={'x': target_pos[0], 'y': target_pos[1]} if target_pos else None,
              previous_distance=prev_distance, distance=dist_before,
              stage=stage, goal_type=goal_type, switch_reason=switch_reason)
        if prev_distance is not None and dist_before >= prev_distance:
            trace(state, role.id, 'opening_step_not_closer', '与上回合相比路径长度未下降',
                  previous_distance=prev_distance, distance=dist_before)
    cmd = move_on_path(state, role, path, reserved, reason)
    stalled = 0
    if goal and goal.get('kind') == goal_kind and goal.get('target_pos') == (list(target_pos) if target_pos else None):
        last = goal.get('last_distance')
        if oscillating:
            stalled = int(goal.get('stalled_rounds') or 0)
        else:
            stalled = int(goal.get('stalled_rounds') or 0) + 1 if last is not None and dist_before >= last else 0
    _store_goal(state, role, stage, goal_kind, goal_type, target_pos, dist_before, stalled, switch_reason,
                last_pos=list(here), prev_pos=(goal or {}).get('last_pos'),
                oscillation_detected=oscillating or bool((goal or {}).get('oscillation_detected')))
    trace(state, role.id, 'opening_worker_tick', '工人本回合目标',
          stage=stage, position={'x': role.pos.x, 'y': role.pos.y},
          goal_type=goal_type, goal_pos=list(target_pos) if target_pos else None,
          distance=dist_before, action='move' if cmd else 'hold', switch_reason=switch_reason)
    return cmd


def _tick(state, role, stage, kind, target_type, target_pos, distance, action, switch_reason, cmd):
    _store_goal(state, role, stage, kind, target_type, target_pos, distance, 0, switch_reason)
    trace(state, role.id, 'opening_worker_tick', '工人本回合目标',
          stage=stage, position={'x': role.pos.x, 'y': role.pos.y},
          goal_type=target_type, goal_pos=list(target_pos) if target_pos else None,
          distance=distance, action=action, switch_reason=switch_reason)
    return cmd


def choose_nearest_mine(role, state, blocked, reserved, want_ores):
    """选可达矿点。第一天优先少走冤枉路：明显更近的矿能打破粘性和占矿。"""
    from .economy import claimed_mines, ore_prices, set_mine_target
    from .economy import vendor_return_steps
    from .opening import adjacent_path
    if state.map_info is None:
        return None, None, 'no_map'
    want = set(want_ores)
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
    want -= banned
    priority = (stockpile & want) - banned
    if priority:
        trace(state, role.id, 'news_stockpile_mine',
              '官方消息预告后续禁采/涨价，首日经济工优先抢收对应矿石',
              ores=sorted(priority), banned=sorted(banned))
        want = priority
    prices = ore_prices(state)
    known = any(prices.get(name, 0) > 0 for name in want)
    occupied = claimed_mines(state, exclude_role_id=role.id)
    goal = _goal(state, role.id)
    sticky = None
    if goal and goal.get('kind') == 'mine' and goal.get('target_pos'):
        sticky = tuple(goal['target_pos'])
    candidates = []
    sticky_cand = None
    for mine in state.map_info.zones:
        if mine.neutral_type not in want:
            continue
        path = adjacent_path(role, mine.pos, blocked | reserved, state)
        relaxed_reserved = False
        if path is None and reserved:
            path = adjacent_path(role, mine.pos, blocked, state)
            relaxed_reserved = path is not None
        if path is None:
            continue
        pos = (mine.pos.x, mine.pos.y)
        length = len(path)
        value = prices.get(mine.neutral_type, 0)
        if known and value > 0 and want != {'stone'}:
            value_score = (length + vendor_return_steps(mine, state, blocked, reserved)) / value
        else:
            value_score = length
        claimed = 1 if pos in occupied else 0
        attack_penalty = 0
        if want <= {'stone'}:
            from .brain import own_station
            from .opening import attack_side_of_front
            base = own_station(state)
            if base is not None and attack_side_of_front(state, base, mine.pos):
                attack_penalty = 1
        row = dict(
            claimed=claimed, value_score=value_score, length=length, mine=mine,
            path=path, pos=pos, relaxed_reserved=relaxed_reserved,
            attack_penalty=attack_penalty,
        )
        candidates.append(row)
        if sticky and pos == sticky:
            sticky_cand = row
    if not candidates:
        return None, None, 'unreachable'

    nearest = min(candidates, key=lambda row: (row['attack_penalty'], row['length'], row['claimed'], row['value_score'], row['pos']))
    best_unclaimed = min(
        (row for row in candidates if not row['claimed']),
        key=lambda row: (row['attack_penalty'], row['value_score'], row['length'], row['pos']),
        default=None,
    )
    if best_unclaimed is None:
        best = nearest
    elif best_unclaimed['length'] - nearest['length'] > CLAIMED_MINE_MAX_DETOUR:
        best = nearest
    else:
        best = best_unclaimed

    if sticky_cand is not None and sticky_cand.get('attack_penalty') and any(not row['attack_penalty'] for row in candidates):
        # 粘性目标在迎敌外侧，改选院内侧矿：语义同“明显更近/更优”
        set_mine_target(state, role.id, best['mine'])
        reason = 'clearly_closer'
        if best['relaxed_reserved']:
            reason += '_relaxed_reserved'
        return best['mine'], best['path'], reason
    if sticky_cand is not None:
        stalled = int((goal or {}).get('stalled_rounds') or 0)
        oscillating = bool((goal or {}).get('oscillation_detected'))
        clearly_closer = best['length'] + MINE_CLEARLY_CLOSER_STEPS <= sticky_cand['length']
        if not clearly_closer and (oscillating or stalled < GOAL_STALL_ROUNDS):
            mine, path = sticky_cand['mine'], sticky_cand['path']
            set_mine_target(state, role.id, mine)
            reason = 'sticky_relaxed_reserved' if sticky_cand['relaxed_reserved'] else 'sticky'
            return mine, path, reason
        mine, path = sticky_cand['mine'], sticky_cand['path']
        if best and best['pos'] != (mine.pos.x, mine.pos.y):
            set_mine_target(state, role.id, best['mine'])
            if clearly_closer:
                reason = 'clearly_closer'
            else:
                reason = 'stalled'
            if best['relaxed_reserved']:
                reason += '_relaxed_reserved'
            return best['mine'], best['path'], reason
        set_mine_target(state, role.id, mine)
        reason = 'sticky_stalled_relaxed_reserved' if sticky_cand['relaxed_reserved'] else 'sticky_stalled'
        return mine, path, reason
    set_mine_target(state, role.id, best['mine'])
    reason = 'nearest'
    if sticky and best['pos'] != sticky:
        reason = 'sticky_gone'
    if best['relaxed_reserved']:
        reason += '_relaxed_reserved'
    return best['mine'], best['path'], reason


def _backpack_full(role):
    cap = role.back_pack_capability or 0
    return bool(cap and len(role.backpack or []) >= cap)


def _metal_count(role, state=None):
    from .economy import worker_metal_count
    return worker_metal_count(role, state)


def _nearest_zone(role, state, blocked, reserved, neutral_type):
    from .opening import adjacent_path
    if state.map_info is None:
        return None, None
    best = None
    for zone in state.map_info.zones:
        if zone.neutral_type != neutral_type:
            continue
        path = adjacent_path(role, zone.pos, blocked | reserved, state)
        if path is None:
            continue
        row = (len(path), zone, path)
        if best is None or row[0] < best[0]:
            best = row
    if best is None:
        return None, None
    return best[1], best[2]


def opening_sell_metal(role, state, blocked, reserved, stage, switch_reason='backpack_full'):
    from .economy import ores_held_for_price_rise
    held = ores_held_for_price_rise(state)
    ores = [n for n in ('copper', 'iron') if n in (role.backpack or []) and n not in held]
    if not ores:
        if held & set(role.backpack or []):
            trace(state, role.id, 'ore_held_for_price_rise', '官方消息预告涨价，这些矿等涨价当天再卖',
                  held=sorted(held & set(role.backpack or [])))
        return None
    zone, path = _nearest_zone(role, state, blocked, reserved, 'vendor')
    if zone is None:
        trace(state, role.id, 'opening_vendor_unreachable', '需要清包但 vendor 不可达')
        return None
    target = (zone.pos.x, zone.pos.y)
    if chebyshev(role.pos, zone.pos) <= 1:
        name = max(ores, key=lambda n: (role.backpack or []).count(n))
        cmd = selected(state, role.id, {'action': 'sell', 'name': name, 'num': (role.backpack or []).count(name)},
                       '出售铜铁')
        return _tick(state, role, stage, 'vendor', 'vendor', target, 0, 'sell', switch_reason, cmd)
    if path:
        return opening_move(state, role, path, reserved, target, '前往小贩出售铜铁',
                            'vendor', 'vendor', switch_reason, stage)
    return None


def opening_shop_voucher(role, state, blocked, reserved, stage, switch_reason='gold_ready'):
    shop, path = _nearest_zone(role, state, blocked, reserved, 'weaponShop')
    if shop is None:
        return None
    target = (shop.pos.x, shop.pos.y)
    if chebyshev(role.pos, shop.pos) <= 1:
        if len(role.backpack or []) >= (role.back_pack_capability or 0):
            for name in ('copper', 'iron', 'stone'):
                if name in (role.backpack or []):
                    cmd = selected(state, role.id, {'action': 'drop', 'name': name}, '腾出背包买券')
                    return _tick(state, role, stage, 'shop', 'weaponShop', target, 0, 'drop', switch_reason, cmd)
            return None
        from .brain import item_cost, upgrade_batch_size
        cost = item_cost('WeaponUpgradeVoucher1', state)
        free = max(0, (role.back_pack_capability or 1) - len(role.backpack or []))
        # 升级计划已扣掉全队已买未用的券，按顺序买够接下来连续需要的 1 级券。
        needed = upgrade_batch_size(state, 'WeaponUpgradeVoucher1', exclude_role_id=role.id)
        num = max(1, min(needed, free or 1, (state.team_our.gold_num or 0) // cost))
        cmd = selected(state, role.id, {'action': 'buy', 'name': 'WeaponUpgradeVoucher1', 'num': num},
                       '批量购买武器升级券')
        if role.role_type == 'pioneer':
            trace(state, role.id, 'pioneer_buys_voucher', '开拓者购买第一张武器升级券')
        return _tick(state, role, stage, 'shop', 'weaponShop', target, 0, 'buy', switch_reason, cmd)
    if path:
        return opening_move(state, role, path, reserved, target, '前往武器商店购买第一张升级券',
                            'shop', 'weaponShop', switch_reason, stage)
    return None


def opening_apply_voucher(role, state, blocked, reserved, stage):
    from .brain import WEAPON_TYPES
    from .opening import adjacent_path
    from .brain import _pending_item_job_targets, _pick_upgradeable
    weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=1)
    if weapon is None:
        return None
    path = adjacent_path(role, weapon.pos, blocked | reserved, state)
    if path is None and chebyshev(role.pos, weapon.pos) > 1:
        return None
    target = (weapon.pos.x, weapon.pos.y)
    if chebyshev(role.pos, weapon.pos) <= 1:
        cmd = selected(state, role.id, {
            'action': 'use', 'name': 'WeaponUpgradeVoucher1',
            'targetPos': [{'x': weapon.pos.x, 'y': weapon.pos.y}],
        }, '使用第一张武器升级券')
        return _tick(state, role, stage, 'weapon', 'rocket', target, 0, 'use', 'have_voucher', cmd)
    if path:
        from .economy import en_route_collect
        detour = en_route_collect(role, state, len(path), '带着第一张升级券回家，顺路采矿；入夜前仍来得及回去使用')
        if detour:
            return _tick(state, role, stage, 'weapon', 'rocket', target, len(path), 'collect', 'have_voucher', detour)
        return opening_move(state, role, path, reserved, target, '前往武器使用升级券',
                            'weapon', 'rocket', 'have_voucher', stage)
    return None


def use_voucher_now(role, state, blocked):
    """与 enforce_held_vouchers 同一规则：开拓者拿到就用；工人身边有可升武器就用，
    否则背包未满、未到回防时间时先继续干活，回防时顺路用。"""
    if 'WeaponUpgradeVoucher1' not in (role.backpack or []):
        return False
    from .brain import WEAPON_TYPES, _pending_item_job_targets, _pick_upgradeable, worker_defers_voucher_use
    if not worker_defers_voucher_use(role, state, blocked):
        return True
    weapon = _pick_upgradeable(state, WEAPON_TYPES, _pending_item_job_targets(state), max_current_level=1)
    return weapon is not None and chebyshev(role.pos, weapon.pos) <= 1


def day1_wall_floor_met(state, floor=7):
    if (state.round_no or 0) >= 70:
        return True
    alive = sum(1 for r in (state.team_our.roles if state.team_our else [])
                if r.role_type == 'wall' and r.health > 0)
    return alive >= floor


def opening_build_weapon(role, state, blocked, reserved, claimed, gold):
    from .brain import own_station, pick_weapon_name
    from .opening import adjacent_path, weapon_candidates
    base = own_station(state)
    if base is None or gold < 25:
        trace(state, role.id, 'opening_no_gold', '武器资金不足；三座火箭未齐前不改去修墙')
        return None, gold
    pending = list(getattr(state, '_opening_pending_weapon_names', []))
    name = pick_weapon_name(state, pending)
    for point in weapon_candidates(state, base, name, extra_positions=claimed):
        if point in claimed or (*point, 'weapon') in state.failed_build_spots:
            continue
        path = adjacent_path(role, Pos(*point), blocked | reserved, state)
        if path is None:
            continue
        claimed.add(point)
        if path:
            state._opening_pending_weapon_names = pending + [name]
            cmd = opening_move(state, role, path, reserved, point, '前往武器施工位',
                               'weapon', 'rocket', 'build_weapons', STAGE_BUILD_WEAPONS)
            return cmd, gold
        cmd = selected(state, role.id, {
            'action': 'build', 'name': name,
            'targetPos': [{'x': point[0], 'y': point[1]}],
        }, '建造武器')
        reserved.add(point)
        state._opening_pending_weapon_names = pending + [name]
        return _tick(state, role, STAGE_BUILD_WEAPONS, 'weapon', 'rocket', point, 0, 'build',
                     'build_weapons', cmd), gold - 25
    return None, gold


def opening_muster(role, state, blocked, reserved, assignments, stage):
    return opening_muster_step(role, state, blocked, reserved, assignments, stage)[1]


def opening_muster_step(role, state, blocked, reserved, assignments, stage):
    """回炮一步。返回 (handled, cmd)：已在炮位时 handled=True、cmd=None，表示本回合原地守炮，
    调用方不能再把这个人派去干别的；handled=False 表示没有可用炮位/路径，由调用方继续安排。"""
    from .opening import builder_dual_rocket, weapon_approach_path
    weapon = assignments.get(role.id)
    if opening_worker_mode(state, role) == 'builder' and (weapon is None or weapon.role_type != 'rocket'):
        # 分到火箭时 weapon_approach_path 本来就优先双火箭共用位；只有没分到火箭时才改去双火箭，
        # 且不抢已经分给队友的那门，否则两人会去同一门炮来回让位。
        night_weapon = builder_dual_rocket(state, role)
        taken = {w.id for rid, w in assignments.items() if rid != role.id and w is not None}
        if night_weapon is not None and night_weapon.id not in taken:
            weapon = night_weapon
    if weapon is None or weapon.health <= 0:
        trace(state, role.id, 'opening_muster_no_weapon', '回防时没有分配到存活武器，本回合无命令',
              stage=stage, assignment_found=weapon is not None,
              weapon_health=None if weapon is None else weapon.health)
        return False, None
    path = weapon_approach_path(role, weapon, blocked, reserved, state)
    target = (weapon.pos.x, weapon.pos.y)
    if path != []:
        # 队友只是路过：不把他们当墙绕出院子一大圈，挡在第一步就原地等一回合。
        from .opening import mobile_walkable
        # reserved 里是队友本回合的落点，同样是会让开的临时占位，直穿路线不看它。
        mobile = weapon_approach_path(role, weapon, mobile_walkable(state, blocked), set(), state)
        if mobile and (path is None or len(mobile) + MUSTER_DETOUR_WAIT <= len(path)):
            first = (mobile[0].x, mobile[0].y)
            teammates = {(r.pos.x, r.pos.y) for r in state.team_our.roles
                         if r.role_type in ('worker', 'pioneer') and r.id != role.id}
            if first in teammates or first in reserved:
                trace(state, role.id, 'muster_wait_teammate', '回炮路上队友挡路，原地等一回合不绕远',
                      blocked_by=list(first), detour=None if path is None else len(path), direct=len(mobile))
                return True, _tick(state, role, stage, 'muster', 'weapon', target, len(mobile), 'hold',
                                   'wait_teammate', None)
            path = mobile
    if path is None:
        trace(state, role.id, 'opening_muster_unreachable', '找不到到分配武器的路径，本回合无命令',
              stage=stage, weapon_id=weapon.id, weapon_pos={'x': weapon.pos.x, 'y': weapon.pos.y})
        return False, None
    if path == []:
        return True, _tick(state, role, stage, 'muster', 'weapon', target, 0, 'hold', 'at_post', None)
    cmd = opening_move(state, role, path, reserved, target, '前往分配武器就位',
                       'muster', 'weapon', 'muster', stage)
    return cmd is not None, cmd


def planned_stone_batch(state, role, blocked, slots, critical):
    """这趟该备多少石头。批次大小由剩余墙位、背包空位和夜前工时共同决定，不用固定常数。"""
    from . import work_orders as wo
    free = wo.free_slots(role)
    stones = wo.stone_count(role)
    rounds = wo.rounds_before_return(state, role, blocked)
    per_wall = wo.ROUNDS_PER_WALL + wo.ROUNDS_PER_STONE
    buildable = max(0, rounds) // per_wall
    if not slots:
        # 已经没有墙位可建：把背包装满，石头留作第二天的城墙材料。
        return stones + (free if free is not None else 0)
    if critical:
        first_batch = min(wo.FIRST_BATCH_WALLS, len(critical), max(1, buildable))
    else:
        first_batch = max(1, min(len(slots), buildable))
    batch = min(wo.stone_needed_for(first_batch), wo.stone_needed_for(len(slots)))
    if free is not None:
        batch = min(batch, stones + free)
    return max(1, batch)


def opening_wall_work(role, state, blocked, reserved, claimed, assignments):
    """建造工城墙状态机：成批采石 → 成批建墙 → 夜前剩余价值最大化。

    不再"采一块修一段"；也不因为"这批当天修不完"就提前回防空转——
    唯一的硬约束是 return_deadline，石头当天用不掉就带到第二天。
    """
    from .opening import (
        MUSTER_BUFFER, assign_weapons, claim_opening_wall, day_rounds_remaining,
        survival_wall_missing,
    )
    from . import work_orders as wo

    orders = wo.compute_work_orders(state, blocked)
    program = orders['wall_program']
    emergency = orders['emergency_defense']['active']
    critical = survival_wall_missing(state)
    slots = [tuple(p) for p in program['target_wall_slots']] or wo.remaining_wall_slots(state)
    if emergency and critical:
        slots = critical
    stones = wo.stone_count(role)
    pack_full = wo.backpack_full(role)
    free = wo.free_slots(role)
    remaining = day_rounds_remaining(state.round_no)
    wall_assignments = assignments or assign_weapons(state)
    previous = wo.builder_state(state, role.id)
    mine_exhausted = wo.collect_failed_last_round(state, role)

    batch = planned_stone_batch(state, role, blocked, slots, critical)
    batch_ready = stones >= batch if batch else stones > 0
    urgent_ready = stones > 0 and remaining <= MUSTER_BUFFER + 6

    # 首批关键墙完成后：比较候选计划，决定这一趟是纯采石、采石加建墙还是先清库存。
    choice = None
    if not critical and slots is not None:
        candidates, choice = wo.plan_candidates(state, role, blocked, reserved)
        trace(state, role.id, 'wall_plan_candidates',
              '首批关键墙已完成，按夜前收益比较候选计划',
              candidate_A=next((c for c in candidates if c['candidate'] == 'GATHER_ONLY'), None),
              candidate_B=next((c for c in candidates if c['candidate'] == 'GATHER_AND_BUILD'), None),
              candidate_C=next((c for c in candidates if c['candidate'] == 'BUILD_FROM_INVENTORY'), None),
              selected_candidate=None if choice is None else choice['candidate'],
              extra_walls_built=None if choice is None else choice['extra_walls_built'],
              stone_carried_to_day2=None if choice is None else choice['stone_carried_to_day2'])

    # BUILD_WALL_BATCH：手上有石、还有墙位就连续修，中途不跳回普通采矿/卖矿/等待。
    # 已进入墙线状态（GO_WALL_LINE/BUILD）时贴着缺口继续砌；刚从矿路过缺口旁且不够批次时仍去凑批，避免一块一跑。
    prefer_gather = choice is not None and choice['candidate'] == 'GATHER_ONLY' and stones <= 0
    build_now = bool(
        stones > 0 and slots and not prefer_gather
        and (pack_full or batch_ready or urgent_ready or mine_exhausted
             or previous in ('BUILD_WALL_BATCH', 'GO_WALL_LINE'))
    )
    if build_now:
        cmd = claim_opening_wall(role, state, slots, blocked, reserved, claimed, wall_assignments)
        if cmd:
            target = None
            if cmd.get('action') in ('build', 'move'):
                tp = cmd.get('targetPos') or [{}]
                target = (tp[0].get('x'), tp[0].get('y'))
            if pack_full:
                switch = 'backpack_full'
            elif urgent_ready:
                switch = 'urgent_wall'
            elif batch_ready:
                switch = 'batch_ready'
            elif mine_exhausted:
                switch = 'mine_exhausted'
            else:
                switch = 'build_batch'
            wall_state = 'BUILD_WALL_BATCH' if cmd.get('action') == 'build' else 'GO_WALL_LINE'
            wo.builder_state(state, role.id, wall_state)
            trace(state, role.id, 'builder_state', '建造工连续施工中',
                  builder_state=wall_state, builder_target=list(target) if target else None,
                  stone_carried=stones, stone_batch_target=batch,
                  inventory_capacity=wo.inventory_capacity(role),
                  inventory_used=wo.inventory_used(role), free_slots=free)
            return _tick(state, role, STAGE_WALL, 'wall', 'wall', target,
                         0 if cmd.get('action') == 'build' else 1,
                         cmd.get('action'), switch, cmd)

    if _metal_count(role, state):
        # 修墙阶段用不上铜铁了（第一天不做第二门升级），背包里有多少都该卖掉换金币，
        # 不能等到背包塞满才想起来卖，不然会一直闲置到入夜白白浪费。
        cmd = opening_sell_metal(role, state, blocked, reserved, STAGE_WALL, 'wall_stage_metal_unused')
        if cmd:
            return cmd
        for name in ('copper', 'iron'):
            if name in (role.backpack or []):
                cmd = selected(state, role.id, {'action': 'drop', 'name': name}, '丢弃铜铁以便采石')
                return _tick(state, role, STAGE_WALL, 'wall', 'drop', None, 0, 'drop',
                             'wall_stage_metal_unused', cmd)

    # GATHER_STONE_BATCH：背包没满就继续采，当天建不完也把石头带到第二天，不提前回防空转。
    if not pack_full and not mine_exhausted:
        mine, path, reason = choose_nearest_mine(role, state, blocked, reserved, ('stone',))
        if mine is not None:
            target = (mine.pos.x, mine.pos.y)
            if slots and not batch_ready:
                switch = 'batch_not_ready'
            elif not slots:
                switch = 'stock_for_day2'
            else:
                switch = reason
            wo.builder_state(state, role.id, 'GO_STONE' if path else 'GATHER_STONE_BATCH')
            trace(state, role.id, 'builder_state', '建造工成批采石',
                  builder_state='GATHER_STONE_BATCH', builder_target=list(target),
                  stone_carried=stones, stone_batch_target=batch,
                  inventory_capacity=wo.inventory_capacity(role),
                  inventory_used=wo.inventory_used(role), free_slots=free,
                  stock_for_day2=not slots)
            if path:
                return opening_move(state, role, path, reserved, target, '前往最近可达石矿继续囤石',
                                    'mine', 'stone', switch, STAGE_WALL)
            cmd = selected(state, role.id, {
                'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}],
            }, '成批采集石头：当天建不完的留作第二天城墙材料')
            return _tick(state, role, STAGE_WALL, 'mine', 'stone', target, 0, 'collect', switch, cmd)
        trace(state, role.id, 'stone_mine_unreachable', '石矿不可达')

    # 还有石头但刚才没能建成：再试一次墙线，避免抱着石头空转。
    if stones > 0 and slots:
        cmd = claim_opening_wall(role, state, slots, blocked, reserved, claimed, wall_assignments)
        if cmd:
            wo.builder_state(state, role.id,
                             'BUILD_WALL_BATCH' if cmd.get('action') == 'build' else 'GO_WALL_LINE')
            return _tick(state, role, STAGE_WALL, 'wall', 'wall', None, 1, cmd.get('action'),
                         'build_batch_retry', cmd)

    wo.builder_state(state, role.id, 'PLAN_NEXT_ACTION')
    from .opening import opening_yard_wait
    wait = opening_yard_wait(role, state, blocked, reserved)
    wait_reason = ('backpack_full_no_buildable_slot' if pack_full else
                   'stone_mine_exhausted' if mine_exhausted else 'no_reachable_work')
    trace(state, role.id, 'builder_wait', '建造工本回合没有有收益的合法动作',
          wait_reason=wait_reason, builder_state='PLAN_NEXT_ACTION',
          stone_carried=stones, stone_batch_target=batch, walls_missing=len(slots),
          inventory_capacity=wo.inventory_capacity(role),
          inventory_used=wo.inventory_used(role), free_slots=free)
    if wait:
        return _tick(state, role, STAGE_WALL, 'wall', 'yard', None, 0, 'move', wait_reason, wait)
    return _tick(state, role, STAGE_WALL, 'wall', 'yard', None, 0, 'hold', wait_reason, None)


def opening_fund_work(role, state, blocked, reserved, gold, cost, helper_walls, claimed, assignments,
                      excluded_buyer_ids=(), stage_label=STAGE_FUND, preferred_buyer_id=None):
    if use_voucher_now(role, state, blocked):
        return opening_apply_voucher(role, state, blocked, reserved, stage_label)
    buyer = preferred_buyer_id if preferred_buyer_id is not None else _voucher_buyer_id(
        state, gold, cost, excluded_ids=excluded_buyer_ids)
    trace(state, role.id, 'voucher_buyer_status', '筹资阶段查看本回合买家判定',
          gold=gold, cost=cost, buyer_id=buyer, is_buyer=(role.id == buyer),
          excluded_ids=sorted(excluded_buyer_ids), preferred_buyer_id=preferred_buyer_id,
          worker_mode=opening_worker_mode(state, role), stage_label=stage_label)
    goal = _goal(state, role.id)
    if (goal and goal.get('kind') == 'vendor' and goal.get('stage') == stage_label
            and _metal_count(role, state) and gold < cost):
        cmd = opening_sell_metal(role, state, blocked, reserved, stage_label, 'sticky')
        if cmd:
            return cmd
    if gold >= cost:
        from .brain import weapon_upgrade_due
        # preferred_buyer 也不能绕过日程（首日第一门已升完不买第二张）
        if role.id == buyer and weapon_upgrade_due(state):
            cmd = opening_shop_voucher(role, state, blocked, reserved, stage_label, 'gold_ready')
            if cmd:
                return cmd
        elif helper_walls and role.role_type == 'worker':
            return opening_wall_work(role, state, blocked, reserved, claimed, assignments)
        elif role.role_type == 'worker':
            from .opening import opening_yard_wait
            wait = opening_yard_wait(role, state, blocked, reserved)
            if wait:
                return _tick(state, role, STAGE_FUND, 'shop', 'hold', None, 0, 'move', 'gold_ready', wait)
            return _tick(state, role, STAGE_FUND, 'shop', 'hold', None, 0, 'hold', 'gold_ready', None)
    from .economy import ore_prices, team_metal_inventory_value
    prices = ore_prices(state)
    known = any(prices.get(n, 0) > 0 for n in ('copper', 'iron'))
    covers = known and gold + team_metal_inventory_value(state) >= cost
    if _metal_count(role, state) and (_backpack_full(role) or covers):
        cmd = opening_sell_metal(role, state, blocked, reserved, stage_label,
                                 'backpack_full' if _backpack_full(role) else 'gold_ready')
        if cmd:
            return cmd
    mine, path, reason = choose_nearest_mine(role, state, blocked, reserved, ('copper', 'iron'))
    if mine is not None:
        if mine.neutral_type == 'stone':
            trace(state, role.id, 'invariant_violation', '筹资阶段选到石矿',
                  invariant_violation='fund_stage_chose_stone')
        target = (mine.pos.x, mine.pos.y)
        if path:
            return opening_move(state, role, path, reserved, target, '前往最近可达铜铁',
                                'mine', mine.neutral_type, reason, stage_label)
        cmd = selected(state, role.id, {
            'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}],
        }, '采集铜铁')
        return _tick(state, role, stage_label, 'mine', mine.neutral_type, target, 0, 'collect', reason, cmd)
    trace(state, role.id, 'no_reachable_metal', '筹资阶段没有可达铜铁')
    return None


def _voucher_buyer_id(state, gold, cost, excluded_ids=(), blocked=None):
    """统一走 economy.pick_weapon_voucher_buyer（完整往返代价 + 跳过任务中开拓者）。
    金币不够或不该再升时不派买家；excluded_ids 为被任务接管、本回合不会执行买券的角色。"""
    if gold < cost:
        return None
    from .brain import weapon_upgrade_due
    from .economy import pick_weapon_voucher_buyer
    from .grid import build_blocked_set
    if blocked is None:
        blocked = build_blocked_set(state)
    buyer = pick_weapon_voucher_buyer(state, blocked)
    if buyer is not None and buyer.id not in excluded_ids:
        return buyer.id
    # 日程上已不该买（如首日第一门已升完）时不要退化成最近人乱买
    if not weapon_upgrade_due(state):
        return None
    candidates = [r for r in state.team_our.roles
                  if r.role_type in ('worker', 'pioneer') and r.health > 0 and r.id not in excluded_ids]
    if not candidates:
        return None
    holders = [r for r in candidates if 'WeaponUpgradeVoucher1' in (r.backpack or [])]
    if holders:
        return holders[0].id
    from .brain import find_zone
    shop = find_zone(state, 'weaponShop')
    if shop is None:
        return candidates[0].id
    return min(candidates, key=lambda r: (chebyshev(r.pos, shop.pos), r.id)).id


def dispatch_opening_role(role, state, stage, blocked, reserved, claimed, assignments, gold, cost, helper_walls,
                          excluded_buyer_ids=(), remaining=None):
    from .tactics import imminent_contact
    worker_mode = opening_worker_mode(state, role)
    if imminent_contact(state) and stage != STAGE_BUILD_WEAPONS:
        handled, cmd = opening_muster_step(role, state, blocked, reserved, assignments, STAGE_MUSTER)
        if handled:
            return cmd  # None 表示已在炮位守着，不再往下派外出工作
    if stage == STAGE_MUSTER:
        return opening_muster(role, state, blocked, reserved, assignments, stage)
    if stage == STAGE_BUILD_WEAPONS:
        if role.role_type != 'worker':
            from .opening import pioneer_stay_clear
            return pioneer_stay_clear(role, state, blocked, reserved, assignments)
        if worker_mode == 'economist':
            trace(state, role.id, 'opening_split_economist',
                  '首日分工：经济工不抢建炮，先采卖铜铁并准备第一张升级券',
                  stage=stage, available_gold=gold, required_gold=cost)
            cmd = opening_fund_work(
                role, state, blocked, reserved, gold, cost, helper_walls=False,
                claimed=claimed, assignments=assignments, excluded_buyer_ids=excluded_buyer_ids,
                stage_label=STAGE_BUILD_WEAPONS, preferred_buyer_id=role.id)
            if cmd:
                return cmd
            trace(state, role.id, 'opening_split_economist_fallback',
                  '经济工当前没有可达铜铁或买券路径，临时帮忙补建武器')
        cmd, _gold = opening_build_weapon(role, state, blocked, reserved, claimed, gold)
        return cmd
    if stage == STAGE_FUND:
        if role.role_type == 'pioneer':
            if 'WeaponUpgradeVoucher1' in (role.backpack or []):
                return opening_apply_voucher(role, state, blocked, reserved, STAGE_FUND)
            if gold >= cost and role.id == _voucher_buyer_id(state, gold, cost, excluded_ids=excluded_buyer_ids):
                trace(state, role.id, 'voucher_buyer_pick', '开拓者被选为第一张升级券买家')
                return opening_shop_voucher(role, state, blocked, reserved, STAGE_FUND, 'gold_ready')
            if gold < cost:
                trace(state, role.id, 'pioneer_voucher_wait_gold', '金币不足，开拓者不空等买券')
            from .opening import pioneer_stay_clear
            return pioneer_stay_clear(role, state, blocked, reserved, assignments)
        return opening_fund_work(role, state, blocked, reserved, gold, cost, helper_walls, claimed, assignments,
                                 excluded_buyer_ids=excluded_buyer_ids)
    if stage == STAGE_APPLY:
        if 'WeaponUpgradeVoucher1' in (role.backpack or []):
            return opening_apply_voucher(role, state, blocked, reserved, STAGE_APPLY)
        if role.role_type == 'worker' and helper_walls:
            return opening_wall_work(role, state, blocked, reserved, claimed, assignments)
        from .brain import weapon_upgrade_due
        if role.role_type == 'worker' and gold >= cost and weapon_upgrade_due(state):
            return opening_shop_voucher(role, state, blocked, reserved, STAGE_APPLY, 'gold_ready')
        return opening_muster(role, state, blocked, reserved, assignments, STAGE_APPLY)
    if stage == STAGE_WALL:
        if use_voucher_now(role, state, blocked):
            return opening_apply_voucher(role, state, blocked, reserved, STAGE_WALL)
        buyer_id = _voucher_buyer_id(
            state, gold, cost, excluded_ids=excluded_buyer_ids, blocked=blocked)
        if role.role_type != 'worker':
            if role.id == buyer_id and 'stone' not in (role.backpack or []):
                trace(state, role.id, 'voucher_buyer_pick', '生存墙阶段由统一买家买火箭升级券',
                      available_gold=gold, required_gold=cost)
                cmd = opening_shop_voucher(role, state, blocked, reserved, STAGE_WALL, 'wall_stage_buyer')
                if cmd:
                    return cmd
            elif gold < cost:
                trace(state, role.id, 'pioneer_voucher_wait_gold',
                      '生存墙阶段金币不足，开拓者不抢工人采矿，只等待任务金币或墙后备用',
                      available_gold=gold, required_gold=cost)
            from .opening import pioneer_day_wait
            handled, cmd = pioneer_day_wait(role, state, blocked, reserved, assignments.get(role.id),
                                            '墙没修完，开拓者让开墙线和院内通道等待')
            if handled:
                return cmd
            return opening_muster(role, state, blocked, reserved, assignments, STAGE_WALL)
        if (worker_mode == 'economist' and 'stone' not in (role.backpack or [])
                and not economist_should_help_wall(state, remaining or 70, role)):
            trace(state, role.id, 'opening_split_economist',
                  '首日分工：经济工继续采卖矿/买券，施工工负责生存墙',
                  stage=stage, available_gold=gold, required_gold=cost)
            return opening_fund_work(
                role, state, blocked, reserved, gold, cost, helper_walls=False,
                claimed=claimed, assignments=assignments, excluded_buyer_ids=excluded_buyer_ids,
                stage_label=STAGE_WALL, preferred_buyer_id=role.id)
        if worker_mode == 'economist':
            trace(state, role.id, 'opening_split_economist_wall_help',
                  '墙压迫或施工工不可用，经济工临时接管生存墙',
                  stage=stage, remaining=remaining, wall_floor_met=day1_wall_floor_met(state))
        wall_floor_met = day1_wall_floor_met(state)
        if role.id == buyer_id and wall_floor_met and gold >= cost:
            trace(state, role.id, 'worker_wall_stage_voucher_attempt',
                  '生存墙达标且金币够，由统一买家买券',
                  available_gold=gold, required_gold=cost)
            cmd = opening_shop_voucher(role, state, blocked, reserved, STAGE_WALL, 'wall_floor_met_buyer')
            if cmd:
                return cmd
        else:
            trace(state, role.id, 'worker_wall_stage_voucher_wait',
                  '生存墙阶段暂不买券，继续修墙' if not wall_floor_met else '非指定买家或金币不够，继续修墙',
                  available_gold=gold, required_gold=cost, wall_floor_met=wall_floor_met,
                  buyer_id=buyer_id)
        return opening_wall_work(role, state, blocked, reserved, claimed, assignments)
    return None


def role_rounds_before_due(role, state, blocked, remaining, own_travel):
    """离个人回防截止还剩几回合（<=0 即该回防）。
    同时看开局炮位估时和 defense_due 用的 defense_snapshot，取更早的那个，
    保证这里放人外出的时候，enforce_held_vouchers 等兜底不会判成“该回防”把命令改掉。"""
    from .opening import MUSTER_BUFFER
    from .pioneer_schedule import defense_snapshot
    # 找不到回炮路径时不按炮位估时强行回防（原来回防也会因无路而落空），交给 defense_snapshot 或继续干活。
    own_left = remaining if own_travel is None else remaining - own_travel - MUSTER_BUFFER
    snap = defense_snapshot(role, state, blocked)
    detail = {'snapshot_travel': snap['travel'], 'threat_eta': snap['threatEta'],
              'snapshot_due': snap['defenseDue'], 'own_rounds_left': own_left}
    if snap['pressure']:
        return 0, detail
    if snap['travel'] is None or snap['threatEta'] is None or snap['nightWaveCleared']:
        return own_left, detail
    snap_left = snap['threatEta'] - snap['travel'] - MUSTER_BUFFER
    detail['snapshot_rounds_left'] = snap_left
    return min(own_left, snap_left), detail


def plan_opening_fsm(state):
    from .brain import (
        WEAPON_TYPES, decide_emergency_heal, decide_self_heal, item_cost, own_station,
        plan_pioneer_tasks, is_day_round,
    )
    from .opening import (
        MUSTER_BUFFER, assign_weapons, builder_dual_rocket, day_rounds_remaining, opening_has_voucher,
        weapon_approach_path, live_l2_weapon_count, survival_wall_missing,
    )
    from .tactics import imminent_contact
    base = own_station(state)
    if base is None:
        return {}
    remaining = day_rounds_remaining(state.round_no)
    blocked, reserved = build_blocked_set(state), set()
    state._opening_pending_weapon_names = []
    fighters = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0]
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    gold = state.team_our.gold_num if state.team_our else 0
    cost = item_cost('WeaponUpgradeVoucher1', state)
    stage = resolve_opening_stage(state, remaining=remaining)
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    if stage == STAGE_MUSTER:
        # 进行中任务不能因回防离开任务点；仅剥离 approaching 预约并清掉，避免预约悬空。
        from .pioneer_schedule import clear_reservation, has_task_reservation
        keep = set()
        if state.phase_task:
            keep |= set(task_pioneers)
        for pid in list(task_pioneers):
            if pid in keep:
                continue
            commands.pop(pid, None)
        task_pioneers = keep
        pioneer = next((r for r in fighters if r.role_type == 'pioneer' and r.health > 0), None)
        if pioneer and has_task_reservation(state, pioneer) and not state.phase_task:
            clear_reservation(state, 'muster_clears_approaching')
    assignments = assign_weapons(state, excluded_ids=task_pioneers, persist=True)
    travel = [weapon_approach_path(r, assignments[r.id], blocked, set(), state)
              for r in fighters if r.id in assignments]
    reachable = [len(p) for p in travel if p is not None]
    if travel and not reachable:
        muster_need = remaining + MUSTER_BUFFER
    else:
        muster_need = max(reachable + [0]) + MUSTER_BUFFER
    if imminent_contact(state):
        muster_need = remaining
    stage = resolve_opening_stage(state, remaining=remaining, muster_need=muster_need)
    helper_walls = past_first_upgrade_cutoff(state, remaining) and (
        opening_has_voucher(state) or gold >= cost)
    claimed = set()
    upgraded_count = live_l2_weapon_count(state)
    flags = flags_from_opening_stage(
        stage, gold, cost, opening_has_voucher(state),
        cutoff=past_first_upgrade_cutoff(state, remaining),
        upgraded_count=upgraded_count,
    )
    from . import work_orders as wo
    orders = wo.compute_work_orders(state, blocked, force=True)
    wo.log_work_orders(state, orders)
    for fighter in fighters:
        deadline = wo.return_deadline(state, fighter, blocked)
        trace(state, fighter.id, 'role_deadlines', '本角色统一截止时间',
              return_deadline=deadline,
              wall_deadline=wo.wall_deadline(state, fighter, blocked),
              upgrade_deadline=wo.upgrade_deadline(state, fighter, blocked),
              task_deadline=(wo.task_deadline(state, fighter, blocked)
                             if fighter.role_type == 'pioneer' else None),
              rounds_before_return=wo.rounds_before_return(state, fighter, blocked),
              builder_state=wo.builder_state(state, fighter.id),
              inventory_capacity=wo.inventory_capacity(fighter),
              inventory_used=wo.inventory_used(fighter),
              free_slots=wo.free_slots(fighter),
              stone_carried=wo.stone_count(fighter))
    if stage == STAGE_BUILD_WEAPONS:
        trace(state, None, 'opening_rockets_first', '三座火箭未齐，工人先建炮')
    trace(state, None, 'opening_phase', '第一天状态机', phase=flags['opening_phase'], cycle=opening_cycle(state),
          weapons=len(weapons), alive_weapons=len(weapons), opening_stage=stage,
          wall_goal=0, walls_completed=sum(1 for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0),
          alive_walls=sum(1 for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0),
          wall_missing=survival_wall_missing(state), required_done=upgraded_count >= 1, upgraded_count=upgraded_count,
          upgrade_funded=flags['upgrade_funded'], upgrade_safe=flags['upgrade_safe'],
          rounds_to_night=remaining, rounds_to_defense=remaining,
          opening_commit=state.policy_memory.get('opening_commit'),
          geometry_note='五阶段状态机：建炮→筹资→用券→生存墙→回炮')
    trace(state, None, 'opening_time_budget', '由 opening_stage 派生，不再每回合重算筹资/修墙',
          remaining=remaining, cycle=opening_cycle(state), rounds_to_night=remaining,
          allow_walls=flags['allow_walls'], allow_upgrade=flags['allow_upgrade'],
          allow_sell=flags['allow_sell'], allow_mine=flags['allow_mine'],
          allow_income_mine=flags['allow_income_mine'], allow_stone_mine=flags['allow_stone_mine'],
          gold=gold, upgraded=upgraded_count >= 1, upgraded_count=upgraded_count,
          required_done=flags['required_done'], alive_weapons=len(weapons),
          upgrade_funded=flags['upgrade_funded'], upgrade_safe=flags['upgrade_safe'],
          funding_reason=flags['funding_reason'], fallback_reason=flags['fallback_reason'],
          opening_commit=state.policy_memory.get('opening_commit'),
          opening_stage=stage, can_finish_walls=True, can_finish_critical=True,
          can_finish_survival_walls=True, wall_need=0, wall_deadline=muster_need, sell_trip=None)
    role_travel = {}
    for role, path in zip((r for r in fighters if r.id in assignments), travel):
        role_travel[role.id] = None if path is None else len(path)
    builder_id = opening_worker_roles(state).get('builder')
    builder_role = next((r for r in fighters if r.id == builder_id), None)
    night_weapon = builder_dual_rocket(state, builder_role) if builder_role else None
    if builder_role is not None and night_weapon is not None:
        bpath = weapon_approach_path(builder_role, night_weapon, blocked, set(), state)
        night_travel = None if bpath is None else len(bpath)
        day_travel = role_travel.get(builder_role.id)
        # 白天按当前炮位继续施工；只在走去双火箭位已经来得及的最后窗口才改用夜里估时。
        if night_travel is not None and remaining <= night_travel + MUSTER_BUFFER:
            role_travel[builder_role.id] = night_travel if day_travel is None else max(day_travel, night_travel)
    cashout_commits = state.policy_memory.setdefault(CASHOUT_COMMIT_KEY, {})
    due_latch = state.policy_memory.setdefault(ROLE_DUE_KEY, {})
    holding = set()
    for role in sorted(fighters, key=lambda r: (r.role_type != 'pioneer', r.id)):
        # 回防阶段 task_pioneers 只剩进行中任务的开拓者：离开任务点即失败，同样不能派回炮。
        if role.id in task_pioneers:
            continue
        heal = decide_emergency_heal(role, state)
        if heal:
            commands[role.id] = selected(state, role.id, heal, '低血紧急治疗')
            continue
        # 个人回防截止点：不用全队最远角色统一停工，基地附近的人可以继续干到自己的截止点。
        own_travel = role_travel.get(role.id)
        working_stage = stage not in (STAGE_BUILD_WEAPONS, STAGE_MUSTER)
        due_in, due_detail = (role_rounds_before_due(role, state, blocked, remaining, own_travel)
                              if working_stage else (remaining, {}))
        day_key = (state.round_no or 0) // 130
        # 个人回防一旦触发，当天就锁定：否则走近炮位后估时变宽松又被放出去，来回空转。
        latched = due_latch.get(str(role.id)) == day_key
        role_due = working_stage and (latched or imminent_contact(state) or due_in <= 0)
        if role_due and not latched:
            due_latch[str(role.id)] = day_key
        committed = bool(cashout_commits.get(str(role.id)))
        if committed and (role_due or not working_stage or not _metal_count(role, state)):
            cashout_commits.pop(str(role.id), None)
            committed = False
        # 还没到硬截止点，但背包有铜铁、马上要进最后回防窗口了：这是最后能安全绕一趟小贩的时机，
        # 现在不卖，等 role_due 触发就只能直接回炮，铜铁只能烂在背包里过夜。
        # 一旦出发就锁定到卖完：越靠近小贩绕路越短，每回合重算会把人放回去干别的，来回空转。
        if not role_due and working_stage and _metal_count(role, state):
            zone, vendor_path = _nearest_zone(role, state, blocked, reserved, 'vendor')
            detour = None if zone is None else 2 * len(vendor_path) + 1
            # detour=来回路程+出售，是“去卖再回炮”相对原地回炮多花的回合上界；
            # due_in 不足 detour 时去了也卖不完，直接回炮，不出发后再折返。
            if committed or (detour is not None and detour <= due_in <= detour + CASHOUT_WINDOW):
                cmd = opening_sell_metal(role, state, blocked, reserved, stage,
                                         'muster_cashout_before_night')
                if cmd:
                    cashout_commits[str(role.id)] = True
                    trace(state, role.id, 'muster_cashout_before_night',
                          '快到个人回防截止点，最后一次绕去卖掉背包里的铜铁',
                          own_travel=own_travel, remaining=remaining, rounds_before_due=due_in,
                          detour=detour, committed=committed, **due_detail)
                    commands[role.id] = cmd
                    continue
                cashout_commits.pop(str(role.id), None)
        if role_due:
            handled, cmd = opening_muster_step(role, state, blocked, reserved, assignments, STAGE_MUSTER)
            if handled:
                trace(state, role.id, 'role_muster_early', '个人回防截止点已到，先于全队进入回炮',
                      own_travel=own_travel, remaining=remaining, team_stage=stage,
                      rounds_before_due=due_in, holding=cmd is None, **due_detail)
                if cmd:
                    commands[role.id] = cmd
                else:
                    # 已在炮位：原地守着，不能再往下派外出工作。
                    holding.add(role.id)
                    heal = decide_self_heal(role)
                    if heal:
                        commands[role.id] = selected(state, role.id, heal, '守炮时自救')
                continue
        cmd = dispatch_opening_role(
            role, state, stage, blocked, reserved, claimed, assignments, gold, cost, helper_walls,
            excluded_buyer_ids=task_pioneers, remaining=remaining)
        if cmd:
            if cmd.get('action') == 'buy':
                gold -= item_cost(cmd.get('name') or 'WeaponUpgradeVoucher1', state)
            if cmd.get('action') == 'build' and cmd.get('name') != 'wall':
                gold -= 25
            commands[role.id] = cmd
            continue
        if role.role_type == 'worker' and stage == STAGE_FUND:
            mine, path, reason = choose_nearest_mine(role, state, blocked, reserved, ('copper', 'iron'))
            if mine is None:
                trace(state, role.id, 'worker_no_command', '筹资阶段没有可达铜铁',
                      worker_state='BLOCKED', no_command_reason='metal_unreachable',
                      stage=stage, backpack=list(role.backpack or []),
                      gold=gold, position={'x': role.pos.x, 'y': role.pos.y})
        elif role.role_type == 'worker' and stage not in (STAGE_MUSTER, STAGE_APPLY):
            trace(state, role.id, 'worker_no_command', '状态机未给出命令',
                  worker_state=stage, no_command_reason='no_action',
                  stage=stage, backpack=list(role.backpack or []),
                  gold=gold, position={'x': role.pos.x, 'y': role.pos.y})
        heal = decide_self_heal(role)
        if heal and role.id not in commands:
            commands[role.id] = selected(state, role.id, heal, '自救')
    for role in fighters:
        if role.role_type != 'worker' or role.id in commands or role.id in holding:
            continue
        if stage in (STAGE_FUND, STAGE_WALL) and not imminent_contact(state):
            trace(state, role.id, 'invariant_violation', '白天工人无命令',
                  invariant_violation='worker_idle_with_survival_wall_missing',
                  stage=stage)
    return commands
