"""有预算上限的战术消费：范围炸弹应急、召唤令干扰下一夜对手。"""
from .grid import chebyshev
from .protocol import Pos
from .decision_log import trace, selected

ITEM_COSTS = {'Bomb': 100, 'DizzyWeapon': 100, 'SmallRobotSummonOrder': 20, 'MiddleRobotSummonOrder': 30,
              'LargeRobotSummonOrder': 100, 'BossRobotSummonOrder': 200}
DAILY_SUMMON_LIMIT = 10
DEFENSE_RESERVE = 100


def begin_round(state):
    if not state.team_our or not state.map_info:
        return
    day = (state.round_no or 0) // 130
    if state.policy_memory.get('summon_day') != day:
        state.policy_memory['summon_day'] = day
        state.policy_memory['summon_attempts'] = []
    state.tactical_purchases = set()
    state.bombed_robots = set()
    from .world_intel import ingest_news
    ingest_news(state)
    from .brain import is_day_round, own_station
    if is_day_round(state.round_no):
        state.policy_memory.pop('night_saw_threat', None)
    from .opening import primary_wall_plan, wall_priority
    base = own_station(state)
    if base:
        front = {p for p in primary_wall_plan(state, base) if wall_priority(state, base, p) == 0}
        standing = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0}
        known = {tuple(p) for p in state.policy_memory.get('front_wall_seen', [])} | (standing & front)
        state.policy_memory['front_wall_seen'] = [list(p) for p in sorted(known & front)]
        state.policy_memory['front_wall_breaches'] = [list(p) for p in sorted((known & front) - standing)]


def front_breached(state):
    from .brain import own_station
    base = own_station(state)
    return bool(base and state.policy_memory.get('front_wall_breaches')
                and any(chebyshev(base.pos, r.pos) <= 7 for r in threat_robots(state)))


def threat_robots(state):
    return [r for r in (state.robot.roles if state.robot else [])
            if r.health > 0 and (not r.target_team or r.target_team == state.team_our.type)]


def night_wave_cleared(state):
    """见过本夜威胁且当前没有存活机器人时，转去采矿/修墙/做任务；开局空波次仍守炮。"""
    from .brain import is_day_round
    if is_day_round(state.round_no):
        return False
    living = threat_robots(state)
    if living:
        state.policy_memory['night_saw_threat'] = True
        return False
    return bool(state.policy_memory.get('night_saw_threat'))


def pressure(state):
    from .brain import own_station, max_health
    base = own_station(state)
    robots = threat_robots(state)
    nearby = [r for r in robots if base and chebyshev(base.pos, r.pos) <= 7]
    return bool(base and (front_breached(state) or len(nearby) >= 4 or (nearby and base.health < max_health(base) * 0.6)))


def bomb_target(state):
    robots = [r for r in threat_robots(state) if r.id not in state.bombed_robots]
    candidates = {(r.pos.x+dx, r.pos.y+dy) for r in robots for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    best = None
    for x, y in sorted(candidates):
        if not (0 <= x < state.map_info.width and 0 <= y < state.map_info.height):
            continue
        hits = [r for r in robots if chebyshev(Pos(x, y), r.pos) <= 1]
        damage = sum(min(100, r.health) for r in hits)
        kills = sum(r.health <= 100 for r in hits)
        if best is None or (kills, damage) > best[:2]:
            best = (kills, damage, Pos(x, y), hits)
    return best if best and (best[0] >= 2 or best[1] >= 200 or (front_breached(state) and best[3])) else None


def dizzy_target(state):
    """高压下的眩晕落点：3×3 命中尽量多的威胁机器人。"""
    robots = [r for r in threat_robots(state) if r.id not in state.bombed_robots]
    if not robots or not state.map_info:
        return None
    candidates = {(r.pos.x + dx, r.pos.y + dy) for r in robots for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    best = None
    for x, y in sorted(candidates):
        if not (0 <= x < state.map_info.width and 0 <= y < state.map_info.height):
            continue
        hits = [r for r in robots if chebyshev(Pos(x, y), r.pos) <= 1]
        if best is None or len(hits) > len(best[0]):
            best = (hits, Pos(x, y))
    if not best:
        return None
    hits, point = best
    if len(hits) >= 2 or front_breached(state) or pressure(state):
        return hits, point
    return None


def tactical_action(role, state, blocked, reserved, allow_travel=True):
    from .brain import own_station, max_health, item_cost
    from .opening import adjacent_path, move_on_path
    if role.health <= 0:
        return None
    urgent = pressure(state)
    breached = front_breached(state)
    if breached:
        trace(state, role.id, 'front_breached', '正面已建城墙被攻破且敌人逼近，立即使用或购买防御道具', gaps=state.policy_memory['front_wall_breaches'])
    target = bomb_target(state)
    if 'Bomb' in role.backpack and urgent and target:
        _, damage, point, hits = target
        state.bombed_robots.update(r.id for r in hits)
        trace(state, role.id, 'emergency_bomb', '高防守压力下使用3×3炸弹，不把范围炸弹当作全图清除', expected_damage=damage, targets=[r.id for r in hits])
        return selected(state, role.id, {'action': 'use', 'name': 'Bomb', 'targetPos': [{'x': point.x, 'y': point.y}]}, '对密集机器人使用范围炸弹')
    stun = dizzy_target(state) if urgent else None
    if 'DizzyWeapon' in role.backpack and stun:
        hits, point = stun
        state.bombed_robots.update(r.id for r in hits)
        trace(state, role.id, 'emergency_dizzy', '高防守压力下使用眩晕法宝，不在平时消耗', targets=[r.id for r in hits])
        return selected(state, role.id, {'action': 'use', 'name': 'DizzyWeapon', 'targetPos': [{'x': point.x, 'y': point.y}]}, '高压下眩晕附近机器人')
    if breached and 'WallFixer' in role.backpack:
        damaged = [r for r in state.team_our.roles if r.role_type == 'wall' and 0 < r.health < max_health(r)*0.8
                   and chebyshev(role.pos, r.pos) <= 1 and ('repair', r.id) not in state.tactical_purchases]
        if damaged:
            wall = min(damaged, key=lambda r: r.health/max_health(r))
            state.tactical_purchases.add(('repair', wall.id))
            return selected(state, role.id, {'action': 'use', 'name': 'WallFixer', 'targetPos': [{'x': wall.pos.x, 'y': wall.pos.y}]}, '正面破口告急，修复附近仍存活的城墙')
    attempts = state.policy_memory.setdefault('summon_attempts', [])
    cycle_round = (state.round_no or 0) % 130
    owned_order = next((name for name in reversed(tuple(ITEM_COSTS)) if name.endswith('SummonOrder') and name in role.backpack), None)
    # 最后一夜之后使用无法影响下一波，故只在有后续夜晚的白天使用。
    if owned_order and cycle_round < 70 and (state.round_no or 0) < 1240 and len(attempts) < DAILY_SUMMON_LIMIT and not urgent:
        key = [state.round_no, role.id]
        if key not in attempts:
            attempts.append(key)  # 按发送次数保守计限额；失败也不透支额度。
        return selected(state, role.id, {'action': 'use', 'name': owned_order}, '消耗召唤令，增加对手下一夜机器人数量')
    base = own_station(state)
    weapons = [r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
    reserve = DEFENSE_RESERVE + max(0, 3-len(weapons))*25
    item = None
    all_backpacks = [i for r in state.team_our.roles for i in r.backpack]
    if urgent and target and 'Bomb' not in all_backpacks and 'Bomb' not in state.tactical_purchases:
        item = 'Bomb'
    elif urgent and stun and 'DizzyWeapon' not in all_backpacks and 'DizzyWeapon' not in state.tactical_purchases:
        item = 'DizzyWeapon'
    elif (cycle_round < 70 and (state.round_no or 0) < 1240 and base and base.health >= max_health(base)*0.7
          and len(weapons) >= 3 and sum(r.role_type == 'wall' for r in state.team_our.roles) >= 6
          and all((r.level or 1) >= 2 for r in weapons)
          and not urgent and role.id not in state.worker_item_jobs
          and not any(i.endswith('SummonOrder') for i in all_backpacks)
          and not any(isinstance(i, str) and i.endswith('SummonOrder') for i in state.tactical_purchases)
          and len(attempts) < DAILY_SUMMON_LIMIT):
        # 每次干扰最多花剩余金币25%，并给防守留下至少100金币。
        cap = min(state.team_our.gold_num // 4, state.team_our.gold_num-reserve)
        item = next((n for n in ('BossRobotSummonOrder', 'LargeRobotSummonOrder', 'MiddleRobotSummonOrder', 'SmallRobotSummonOrder')
                     if item_cost(n, state) <= cap), None)
    if item is None or state.team_our.gold_num < item_cost(item, state) or len(role.backpack) >= role.back_pack_capability:
        return None
    shops = [z for z in state.map_info.zones if z.neutral_type == 'weaponShop']
    choices = [(adjacent_path(role, shop.pos, blocked | reserved, state), shop) for shop in shops]
    choices = [(p, shop) for p, shop in choices if p is not None]
    if not choices:
        return None
    path, _ = min(choices, key=lambda pair: len(pair[0]))
    labels = {
        'Bomb': ('前往商店购买应急炸弹', '购买应急炸弹'),
        'DizzyWeapon': ('前往商店购买眩晕法宝', '购买眩晕法宝'),
    }
    travel_reason, buy_reason = labels.get(item, ('防守预算充足，前往商店购买干扰道具', '购买机器人召唤令干扰对手'))
    if path:
        # 正在防守的操控者不离炮购物；白天也不在临夜发动长途购物。
        if not allow_travel or cycle_round >= 70 or len(path)*2+5 >= 70-cycle_round:
            return None
        return move_on_path(state, role, path, reserved, travel_reason)
    state.tactical_purchases.add(item)
    trace(state, role.id, 'tactical_purchase', '战术消费', item=item, cost=item_cost(item, state), defense_reserve=reserve, pressure=urgent)
    return selected(state, role.id, {'action': 'buy', 'name': item, 'num': 1}, buy_reason)
