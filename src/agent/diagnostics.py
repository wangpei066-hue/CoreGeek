"""从快照计算可验证的复盘指标；只读，不调用决策函数或修改策略记忆。"""
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path


def _source_version():
    digest = sha256()
    for name in ('brain.py', 'opening.py', 'economy.py', 'tactics.py', 'decision_log.py', 'diagnostics.py'):
        try:
            digest.update((Path(__file__).parent / name).read_bytes())
        except OSError:
            return 'unavailable'
    return digest.hexdigest()[:12]


SOURCE_VERSION = _source_version()


def diagnostics(state, commands, previous, comparable, previous_commands):
    if not state.team_our or not state.map_info:
        return {'alerts': [], 'note': '缺少队伍或地图，无法计算诊断指标'}
    from .brain import own_station, max_health, WEAPON_TYPES
    from .opening import primary_wall_plan, assign_weapons, station_path, adjacent_path
    from .grid import build_blocked_set, chebyshev
    from .tactics import threat_robots
    roles = state.team_our.roles
    base = own_station(state)
    walls = {(r.pos.x, r.pos.y): r for r in roles if r.role_type == 'wall' and r.health > 0}
    primary = primary_wall_plan(state, base) if base else []
    def wall_status(points):
        return {'planned': len(points), 'built': sum(p in walls for p in points),
                'missing': [list(p) for p in points if p not in walls],
                'needs_upgrade': [list(p) for p in points if p in walls and (walls[p].level or 1) < 2],
                'needs_repair': [list(p) for p in points if p in walls and walls[p].health < max_health(walls[p])*0.8]}
    prices = {i.name: i.price for i in state.vendor_shop_list}
    blocked = build_blocked_set(state)
    assignments = assign_weapons(state)
    cycle = (state.round_no or 0) % 130
    alerts, actors, weapons = [], [], []
    consecutive = comparable and state.round_no == previous['round'] + 1
    for key, result in state.last_round_role_action_results.items():
        if result is False:
            alerts.append({'code': 'ACTION_FAILED', 'command_key': key, 'command': previous_commands.get(key),
                           'message': '系统报告动作失败；没有逐指令原因时不猜测'})
    for r in roles:
        if r.role_type not in ('worker', 'pioneer'):
            continue
        ores = Counter(i for i in r.backpack if i in ('stone', 'iron', 'copper'))
        value = sum(n*prices.get(i, 0) for i, n in ores.items())
        w = assignments.get(r.id)
        path = station_path(r, w, blocked, state) if w else None
        vendors = [adjacent_path(r, z.pos, blocked, state) for z in state.map_info.zones if z.neutral_type == 'vendor']
        distances = [len(p) for p in vendors if p is not None]
        entry = {'id': r.id, 'ore_counts': dict(ores), 'quoted_ore_value': value,
                 'unknown_price_ores': [i for i in ores if i not in prices],
                 'capacity': r.back_pack_capability, 'used_slots': len(r.backpack),
                 'assigned_weapon': w.id if w else None, 'return_steps': len(path) if path is not None else None,
                 'vendor_steps': min(distances) if distances else None,
                 'at_weapon': bool(w and chebyshev(r.pos, w.pos) <= 1),
                 'selling_committed': r.id in state.policy_memory.get('selling_roles', [])}
        actors.append(entry)
        if r.health > 0 and cycle >= 70 and not entry['at_weapon']:
            alerts.append({'code': 'NIGHT_UNSTATIONED', 'role_id': r.id, 'message': '夜间未到武器旁', 'return_steps': entry['return_steps']})
        if value >= 25 and ((base and base.health < max_health(base)*0.8) or cycle >= 50):
            alerts.append({'code': 'ORE_AT_RISK', 'role_id': r.id, 'message': '防守期或基地受损时仍携带高价值矿石', 'quoted_value': value})
        if comparable:
            old = previous['roles'].get(r.id)
            if consecutive and old and old['position'] == asdict(r.pos) and previous_commands.get(r.id, {}).get('action') == 'move':
                alerts.append({'code': 'MOVE_NO_PROGRESS', 'role_id': r.id, 'message': '上一回合发出移动，但位置未变化；结合执行反馈检查阻挡'})
            if old and old['health'] > 0 and r.health <= 0:
                alerts.append({'code': 'ROLE_DIED', 'role_id': r.id, 'message': '观察到角色死亡', 'previous_backpack': old['backpack']})
    robots = threat_robots(state)
    for w in roles:
        if w.role_type not in WEAPON_TYPES or w.health <= 0:
            continue
        targets = [r.id for r in robots if chebyshev(w.pos, r.pos) <= w.attack_range]
        nearby = [r.id for r in roles if r.role_type in ('worker', 'pioneer') and r.health > 0 and chebyshev(w.pos, r.pos) <= 1]
        cmd = commands.get(w.id, {})
        weapons.append({'id': w.id, 'type': w.role_type, 'pos': asdict(w.pos), 'level': w.level,
                        'health': w.health, 'range': w.attack_range, 'cooldown': w.cooldown,
                        'nearby_operators': nearby, 'in_range_robots': targets, 'command': cmd})
        if cycle >= 70 and targets and not cmd and not (w.role_type == 'rocket' and w.cooldown):
            alerts.append({'code': 'WEAPON_NOT_FIRING', 'weapon_id': w.id, 'message': '射程内有敌人且武器未冷却，但本回合没有攻击；需结合角色道具/移动指令判断'})
    primary_status = wall_status(primary)
    weapon_upgrade_costs = []
    for weapon in (r for r in roles if r.role_type in WEAPON_TYPES and r.health > 0 and (r.level or 1) < 2):
        from .brain import voucher_for, item_cost
        voucher, _ = voucher_for('weapon', weapon.level or 1)
        weapon_upgrade_costs.append({'weapon_id': weapon.id, 'type': weapon.role_type,
                                     'level': weapon.level or 1, 'voucher': voucher,
                                     'cost': item_cost(voucher, state)})
    upgrade_need = sum(item['cost'] for item in weapon_upgrade_costs)
    upgrade_deadline = {'target_round': 330, 'rounds_left': max(0, 330-(state.round_no or 0)),
                        'pending': weapon_upgrade_costs, 'gold_required': upgrade_need,
                        'gold_available': state.team_our.gold_num,
                        'funding_gap': max(0, upgrade_need-state.team_our.gold_num),
                        'on_target': not weapon_upgrade_costs}
    if weapon_upgrade_costs and (state.round_no or 0) >= 260:
        alerts.append({'code': 'WEAPON_UPGRADE_DEADLINE', 'message': '第三夜前仍有一级武器',
                       'rounds_left': upgrade_deadline['rounds_left'],
                       'funding_gap': upgrade_deadline['funding_gap'], 'pending': weapon_upgrade_costs})
    if primary_status['missing']:
        alerts.append({'code': 'PRIMARY_GAPS', 'message': '第一层防线存在缺口', 'positions': primary_status['missing']})
    return {'source_version': SOURCE_VERSION, 'team_id': state.team_our.team_id, 'side': state.team_our.type,
            'day': (state.round_no or 0)//130+1, 'cycle_round': cycle,
            'rounds_to_night': max(0, 70-cycle), 'score': state.team_our.total_score,
            'gold_delta': state.team_our.gold_num-previous['gold'] if comparable and previous['gold'] is not None else None,
            'delta_note': '净变化可能包含交易、任务及其他来源，不能直接归因于某次卖矿。',
            'primary': primary_status,
            'weapon_upgrade_deadline': upgrade_deadline,
            'actors': actors, 'weapons': weapons, 'robots_by_type': dict(Counter(r.role_type for r in robots)),
            'robots_near_base': sum(chebyshev(base.pos, r.pos) <= 7 for r in robots) if base else None,
            'alerts': alerts,
            'initial_context': {'team_id': state.team_our.team_id, 'side': state.team_our.type,
                                'map': asdict(state.map_info), 'vendor_prices': prices,
                                'weapon_prices': {i.name: i.price for i in state.weapon_shop_list}} if not comparable else None}
