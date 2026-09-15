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


def flags_from_opening_stage(stage, gold, cost, has_voucher, cutoff=False):
    funded = has_voucher or gold >= cost
    log_phase = {
        STAGE_BUILD_WEAPONS: '武器',
        STAGE_FUND: '筹资升级',
        STAGE_APPLY: 'APPLY_FIRST_UPGRADE',
        STAGE_WALL: 'SURVIVAL_WALL',
        STAGE_MUSTER: '就位',
    }.get(stage, stage)
    mining = stage == STAGE_FUND and not funded and not cutoff
    flags = {
        'opening_phase': log_phase,
        'allow_walls': stage in (STAGE_WALL, STAGE_APPLY) or (stage == STAGE_FUND and cutoff and funded),
        'allow_upgrade': stage in (STAGE_FUND, STAGE_APPLY) and funded,
        'allow_sell': stage == STAGE_FUND and not cutoff,
        'allow_mine': mining,
        'allow_income_mine': mining,
        'allow_stone_mine': stage == STAGE_WALL,
        'allow_first_upgrade': stage in (STAGE_FUND, STAGE_APPLY) and funded,
        'upgrade_funded': funded,
        'upgrade_safe': stage == STAGE_FUND and funded and not cutoff,
        'required_done': stage in (STAGE_WALL, STAGE_MUSTER),
        'fallback_reason': 'upgrade_cutoff_unfunded' if stage == STAGE_WALL and not funded else None,
        'funding_reason': (
            'have_voucher' if has_voucher else 'gold_ready' if gold >= cost else 'unfunded'
        ),
    }
    if stage == STAGE_WALL:
        flags['allow_upgrade'] = has_voucher
        flags['allow_first_upgrade'] = has_voucher
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
        dist_after = dist_before - 1
        trace(state, role.id, 'opening_step', '向目标前进一步',
              current_position={'x': here[0], 'y': here[1]},
              next_position={'x': nxt[0], 'y': nxt[1]},
              target_position={'x': target_pos[0], 'y': target_pos[1]} if target_pos else None,
              distance_before=dist_before, distance_after=dist_after,
              stage=stage, goal_type=goal_type, switch_reason=switch_reason)
        if dist_after >= dist_before:
            trace(state, role.id, 'opening_step_not_closer', '本步路径长度未下降，仍走完整 BFS 下一步')
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
    prices = ore_prices(state)
    known = any(prices.get(name, 0) > 0 for name in want_ores)
    occupied = claimed_mines(state, exclude_role_id=role.id)
    goal = _goal(state, role.id)
    sticky = None
    if goal and goal.get('kind') == 'mine' and goal.get('target_pos'):
        sticky = tuple(goal['target_pos'])
    candidates = []
    sticky_cand = None
    for mine in state.map_info.zones:
        if mine.neutral_type not in want_ores:
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
        if known and value > 0 and want_ores != ('stone',):
            value_score = (length + vendor_return_steps(mine, state, blocked, reserved)) / value
        else:
            value_score = length
        claimed = 1 if pos in occupied else 0
        row = dict(
            claimed=claimed, value_score=value_score, length=length, mine=mine,
            path=path, pos=pos, relaxed_reserved=relaxed_reserved,
        )
        candidates.append(row)
        if sticky and pos == sticky:
            sticky_cand = row
    if not candidates:
        return None, None, 'unreachable'

    nearest = min(candidates, key=lambda row: (row['length'], row['claimed'], row['value_score'], row['pos']))
    best_unclaimed = min(
        (row for row in candidates if not row['claimed']),
        key=lambda row: (row['value_score'], row['length'], row['pos']),
        default=None,
    )
    if best_unclaimed is None:
        best = nearest
    elif best_unclaimed['length'] - nearest['length'] > CLAIMED_MINE_MAX_DETOUR:
        best = nearest
    else:
        best = best_unclaimed

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


def _metal_count(role):
    return sum(1 for n in (role.backpack or []) if n in ('copper', 'iron'))


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
    ores = [n for n in ('copper', 'iron') if n in (role.backpack or [])]
    if not ores:
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
        cmd = selected(state, role.id, {'action': 'buy', 'name': 'WeaponUpgradeVoucher1', 'num': 1},
                       '购买第一张武器升级券')
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
        return opening_move(state, role, path, reserved, target, '前往武器使用升级券',
                            'weapon', 'rocket', 'have_voucher', stage)
    return None


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
    pending = []
    name = pick_weapon_name(state, pending)
    for point in weapon_candidates(state, base, name, extra_positions=claimed):
        if point in claimed or (*point, 'weapon') in state.failed_build_spots:
            continue
        path = adjacent_path(role, Pos(*point), blocked | reserved, state)
        if path is None:
            continue
        claimed.add(point)
        if path:
            cmd = opening_move(state, role, path, reserved, point, '前往武器施工位',
                               'weapon', 'rocket', 'build_weapons', STAGE_BUILD_WEAPONS)
            return cmd, gold
        cmd = selected(state, role.id, {
            'action': 'build', 'name': pick_weapon_name(state, []),
            'targetPos': [{'x': point[0], 'y': point[1]}],
        }, '建造武器')
        reserved.add(point)
        return _tick(state, role, STAGE_BUILD_WEAPONS, 'weapon', 'rocket', point, 0, 'build',
                     'build_weapons', cmd), gold - 25
    return None, gold


def opening_muster(role, state, blocked, reserved, assignments, stage):
    from .opening import weapon_approach_path
    weapon = assignments.get(role.id)
    if weapon is None or weapon.health <= 0:
        return None
    path = weapon_approach_path(role, weapon, blocked, reserved, state)
    target = (weapon.pos.x, weapon.pos.y)
    if path is None:
        return None
    if path == []:
        return _tick(state, role, stage, 'muster', 'weapon', target, 0, 'hold', 'at_post', None)
    return opening_move(state, role, path, reserved, target, '前往分配武器就位',
                        'muster', 'weapon', 'muster', stage)


def opening_wall_work(role, state, blocked, reserved, claimed, assignments):
    """首日生存墙优先：少量石头也先补关键缺口，避免囤石拖过入夜。"""
    from .opening import STONE_BATCH, claim_opening_wall, survival_wall_missing
    missing = survival_wall_missing(state)
    stones = (role.backpack or []).count('stone')
    cap = role.back_pack_capability or 0
    pack_full = bool(cap and len(role.backpack or []) >= cap)
    # 只备够这名工人这趟真正用得上的量：不超过 STONE_BATCH，也不超过缺口数。
    batch_target = min(STONE_BATCH, len(missing)) if missing else 0
    urgent_ready = stones > 0
    batch_ready = (stones >= batch_target if batch_target else stones > 0) or urgent_ready
    mine_exhausted = False
    if missing and stones > 0 and not pack_full and not batch_ready:
        mine, path, reason = choose_nearest_mine(role, state, blocked, reserved, ('stone',))
        if mine is not None:
            target = (mine.pos.x, mine.pos.y)
            if path:
                return opening_move(state, role, path, reserved, target, '前往最近可达石矿继续囤石',
                                    'mine', 'stone', reason, STAGE_WALL)
            cmd = selected(state, role.id, {
                'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}],
            }, '继续采集石头，攒够一批再建墙')
            return _tick(state, role, STAGE_WALL, 'mine', 'stone', target, 0, 'collect',
                         'batch_not_ready', cmd)
        mine_exhausted = True
        trace(state, role.id, 'stone_mine_unreachable', '石矿不可达，带着手上的石头去建墙')
    if stones > 0 and missing and (pack_full or batch_ready or mine_exhausted):
        cmd = claim_opening_wall(role, state, missing, blocked, reserved, claimed, assignments)
        if cmd:
            target = None
            if cmd.get('action') in ('build', 'move'):
                tp = cmd.get('targetPos') or [{}]
                target = (tp[0].get('x'), tp[0].get('y'))
            if urgent_ready:
                switch = 'urgent_wall'
            else:
                switch = 'batch_ready' if batch_ready else ('backpack_full' if pack_full else 'mine_exhausted')
            return _tick(state, role, STAGE_WALL, 'wall', 'wall', target, 0 if cmd.get('action') == 'build' else 1,
                         cmd.get('action'), switch, cmd)
    if _metal_count(role):
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
    if missing and not pack_full and (not batch_ready or stones == 0):
        mine, path, reason = choose_nearest_mine(role, state, blocked, reserved, ('stone',))
        if mine is not None:
            target = (mine.pos.x, mine.pos.y)
            if path:
                return opening_move(state, role, path, reserved, target, '前往最近可达石矿',
                                    'mine', 'stone', reason, STAGE_WALL)
            cmd = selected(state, role.id, {
                'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}],
            }, '采集石头')
            return _tick(state, role, STAGE_WALL, 'mine', 'stone', target, 0, 'collect', reason, cmd)
        trace(state, role.id, 'stone_mine_unreachable', '石矿不可达')
    from .opening import opening_yard_wait
    wait = opening_yard_wait(role, state, blocked, reserved)
    if wait:
        return _tick(state, role, STAGE_WALL, 'wall', 'yard', None, 0, 'move', 'wait_in_yard', wait)
    return _tick(state, role, STAGE_WALL, 'wall', 'yard', None, 0, 'hold', 'wait_in_yard', None)


def opening_fund_work(role, state, blocked, reserved, gold, cost, helper_walls, claimed, assignments,
                      excluded_buyer_ids=()):
    if 'WeaponUpgradeVoucher1' in (role.backpack or []):
        return opening_apply_voucher(role, state, blocked, reserved, STAGE_FUND)
    buyer = _voucher_buyer_id(state, gold, cost, excluded_ids=excluded_buyer_ids)
    goal = _goal(state, role.id)
    if (goal and goal.get('kind') == 'vendor' and goal.get('stage') == STAGE_FUND
            and _metal_count(role) and gold < cost):
        cmd = opening_sell_metal(role, state, blocked, reserved, STAGE_FUND, 'sticky')
        if cmd:
            return cmd
    if gold >= cost:
        if role.id == buyer:
            cmd = opening_shop_voucher(role, state, blocked, reserved, STAGE_FUND, 'gold_ready')
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
    if _metal_count(role) and (_backpack_full(role) or covers):
        cmd = opening_sell_metal(role, state, blocked, reserved, STAGE_FUND,
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
                                'mine', mine.neutral_type, reason, STAGE_FUND)
        cmd = selected(state, role.id, {
            'action': 'collect', 'targetPos': [{'x': mine.pos.x, 'y': mine.pos.y}],
        }, '采集铜铁')
        return _tick(state, role, STAGE_FUND, 'mine', mine.neutral_type, target, 0, 'collect', reason, cmd)
    trace(state, role.id, 'no_reachable_metal', '筹资阶段没有可达铜铁')
    return None


def _voucher_buyer_id(state, gold, cost, excluded_ids=()):
    """excluded_ids：本回合被开拓者自进化任务接管、根本不会被 dispatch 的角色。
    选中它们当买家等于没人买——它们在主循环里直接 continue，永远不会执行到这笔购买。"""
    if gold < cost:
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
                          excluded_buyer_ids=()):
    from .tactics import imminent_contact
    if imminent_contact(state) and stage != STAGE_BUILD_WEAPONS:
        cmd = opening_muster(role, state, blocked, reserved, assignments, STAGE_MUSTER)
        if cmd:
            return cmd
    if stage == STAGE_MUSTER:
        return opening_muster(role, state, blocked, reserved, assignments, stage)
    if stage == STAGE_BUILD_WEAPONS:
        if role.role_type != 'worker':
            from .opening import pioneer_stay_clear
            return pioneer_stay_clear(role, state, blocked, reserved, assignments)
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
        if role.role_type == 'worker' and gold >= cost:
            return opening_shop_voucher(role, state, blocked, reserved, STAGE_APPLY, 'gold_ready')
        return opening_muster(role, state, blocked, reserved, assignments, STAGE_APPLY)
    if stage == STAGE_WALL:
        if 'WeaponUpgradeVoucher1' in (role.backpack or []):
            return opening_apply_voucher(role, state, blocked, reserved, STAGE_WALL)
        if role.role_type != 'worker':
            if gold >= cost and 'stone' not in (role.backpack or []):
                trace(state, role.id, 'voucher_buyer_pick', '生存墙阶段开拓者空档并行购买火箭升级券',
                      available_gold=gold, required_gold=cost)
                cmd = opening_shop_voucher(role, state, blocked, reserved, STAGE_WALL, 'pioneer_parallel_voucher')
                if cmd:
                    return cmd
            elif gold < cost:
                trace(state, role.id, 'pioneer_voucher_wait_gold',
                      '生存墙阶段金币不足，开拓者不抢工人采矿，只等待任务金币或墙后备用',
                      available_gold=gold, required_gold=cost)
            return opening_muster(role, state, blocked, reserved, assignments, STAGE_WALL)
        if day1_wall_floor_met(state) and gold >= cost:
            cmd = opening_shop_voucher(role, state, blocked, reserved, STAGE_WALL, 'wall_floor_met_backup_voucher')
            if cmd:
                return cmd
        return opening_wall_work(role, state, blocked, reserved, claimed, assignments)
    return None


def plan_opening_fsm(state):
    from .brain import (
        WEAPON_TYPES, decide_emergency_heal, decide_self_heal, item_cost, own_station,
        plan_pioneer_tasks, is_day_round,
    )
    from .opening import (
        MUSTER_BUFFER, assign_weapons, day_rounds_remaining, movement_avoid, opening_has_voucher,
        weapon_approach_path, live_l2_weapon_count, survival_wall_missing,
    )
    from .tactics import imminent_contact
    base = own_station(state)
    if base is None:
        return {}
    remaining = day_rounds_remaining(state.round_no)
    blocked, reserved = build_blocked_set(state) | movement_avoid(state), set()
    fighters = [r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer') and r.health > 0]
    weapons = [r for r in state.team_our.roles if r.role_type in WEAPON_TYPES and r.health > 0]
    gold = state.team_our.gold_num if state.team_our else 0
    cost = item_cost('WeaponUpgradeVoucher1', state)
    stage = resolve_opening_stage(state, remaining=remaining)
    commands, task_pioneers = plan_pioneer_tasks(state, blocked, reserved)
    if stage == STAGE_MUSTER:
        for pid in list(task_pioneers):
            commands.pop(pid, None)
        task_pioneers.clear()
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
    )
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
    for role in sorted(fighters, key=lambda r: (r.role_type != 'pioneer', r.id)):
        if role.id in task_pioneers and stage != STAGE_MUSTER:
            continue
        heal = decide_emergency_heal(role, state)
        if heal:
            commands[role.id] = selected(state, role.id, heal, '低血紧急治疗')
            continue
        # 个人回防截止点：不用全队最远角色统一停工，基地附近的人可以继续干到自己的截止点。
        own_travel = role_travel.get(role.id)
        role_deadline = remaining if own_travel is None else own_travel + MUSTER_BUFFER
        role_due = stage not in (STAGE_BUILD_WEAPONS, STAGE_MUSTER) and (
            imminent_contact(state) or remaining <= role_deadline)
        # 还没到硬截止点，但背包有铜铁、马上要进最后回防窗口了：这是最后能安全绕一趟小贩的时机，
        # 现在不卖，等 role_due 触发就只能直接回炮，铜铁只能烂在背包里过夜。
        if (not role_due and stage not in (STAGE_BUILD_WEAPONS, STAGE_MUSTER)
                and not imminent_contact(state) and _metal_count(role)):
            zone, vendor_path = _nearest_zone(role, state, blocked, reserved, 'vendor')
            if zone is not None:
                detour = 2 * len(vendor_path) + 1
                if remaining <= role_deadline + detour:
                    cmd = opening_sell_metal(role, state, blocked, reserved, stage,
                                             'muster_cashout_before_night')
                    if cmd:
                        trace(state, role.id, 'muster_cashout_before_night',
                              '快到个人回防截止点，最后一次绕去卖掉背包里的铜铁',
                              own_travel=own_travel, remaining=remaining, role_deadline=role_deadline,
                              detour=detour)
                        commands[role.id] = cmd
                        continue
        if role_due:
            cmd = opening_muster(role, state, blocked, reserved, assignments, STAGE_MUSTER)
            if cmd:
                trace(state, role.id, 'role_muster_early', '个人回防截止点已到，先于全队进入回炮',
                      own_travel=own_travel, remaining=remaining, team_stage=stage)
                commands[role.id] = cmd
                continue
        cmd = dispatch_opening_role(
            role, state, stage, blocked, reserved, claimed, assignments, gold, cost, helper_walls,
            excluded_buyer_ids=task_pioneers)
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
        if role.role_type != 'worker' or role.id in commands:
            continue
        if stage in (STAGE_FUND, STAGE_WALL) and not imminent_contact(state):
            trace(state, role.id, 'invariant_violation', '白天工人无命令',
                  invariant_violation='worker_idle_with_survival_wall_missing',
                  stage=stage)
    return commands
