"""第一天状态机：连续回合轨迹，而不是单快照。"""
import unittest

from src.agent.brain import V1Strategy, BasicActionValidator
from src.agent.opening import opening_time_budget, primary_wall_plan, plan_opening
from src.agent.opening_schedule import (
    FIRST_UPGRADE_CUTOFF, STAGE_APPLY, STAGE_FUND, STAGE_MUSTER, STAGE_WALL,
    current_opening_stage,
)
from src.agent.grid import build_blocked_set
from src.agent.protocol import Pos, Zone, ShopItem
from test_opening import opening_state
from test_shop_items import make_role


def _rockets(state, level=1):
    state.team_our.roles += [
        make_role(20, 12, 10, 'rocket', level=level, health=1000),
        make_role(21, 12, 8, 'rocket', level=level, health=1000),
        make_role(22, 12, 12, 'rocket', level=level, health=1000),
    ]


def _map(state, quotes=True):
    state.map_info.zones = [
        Zone(Pos(8, 9), 'copper'),
        Zone(Pos(12, 6), 'iron'),
        Zone(Pos(6, 9), 'stone'),
        Zone(Pos(1, 9), 'weaponShop'),
        Zone(Pos(1, 11), 'vendor'),
    ]
    if quotes:
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 3), ShopItem('stone', 1)]
    else:
        state.vendor_shop_list = []
    state.weapon_shop_list = [ShopItem('WeaponUpgradeVoucher1', 100)]
    return state


def apply_opening_commands(state, commands):
    prices = {i.name: i.price for i in state.vendor_shop_list}
    for rid, cmd in commands.items():
        role = next((r for r in state.team_our.roles if r.id == rid), None)
        if role is None or role.health <= 0:
            continue
        action = cmd.get('action')
        if action == 'move':
            role.pos = Pos(**cmd['targetPos'][0])
        elif action == 'collect':
            pos = cmd['targetPos'][0]
            zone = next((z for z in state.map_info.zones
                         if z.pos.x == pos['x'] and z.pos.y == pos['y']), None)
            if zone:
                role.backpack.append(zone.neutral_type)
        elif action == 'build':
            pos = cmd['targetPos'][0]
            name = cmd['name']
            if name == 'wall' and 'stone' in role.backpack:
                role.backpack.remove('stone')
            elif name == 'rocket':
                state.team_our.gold_num -= 25
            state.team_our.roles.append(
                make_role(100 + len(state.team_our.roles), pos['x'], pos['y'], name, level=1, health=1000)
            )
        elif action == 'sell':
            name, num = cmd['name'], int(cmd.get('num') or 1)
            for _ in range(num):
                if name in role.backpack:
                    role.backpack.remove(name)
                    state.team_our.gold_num += prices.get(name, 0)
        elif action == 'buy':
            role.backpack.append(cmd['name'])
            state.team_our.gold_num -= 100
        elif action == 'use' and cmd.get('name') in role.backpack:
            role.backpack.remove(cmd['name'])
            tp = cmd['targetPos'][0]
            for item in state.team_our.roles:
                if item.pos.x == tp['x'] and item.pos.y == tp['y'] and item.role_type == 'rocket' and item.health > 0:
                    item.level = (item.level or 1) + 1
                    break
        elif action == 'drop' and cmd.get('name') in role.backpack:
            role.backpack.remove(cmd['name'])


def ticks_from_state(state, commands):
    events = [e for e in state.decision_events if e.get('code') == 'opening_worker_tick']
    by_id = {e.get('role_id'): e for e in events}
    rows = []
    for rid in (1, 2):
        ev = by_id.get(rid) or {}
        cmd = commands.get(rid) or {}
        role = next(r for r in state.team_our.roles if r.id == rid)
        rows.append({
            'round': state.round_no,
            'worker_id': rid,
            'stage': current_opening_stage(state) or ev.get('stage'),
            'position': (role.pos.x, role.pos.y),
            'goal_type': ev.get('goal_type'),
            'goal_pos': tuple(ev['goal_pos']) if ev.get('goal_pos') else None,
            'distance': ev.get('distance'),
            'action': cmd.get('action') or ev.get('action'),
            'switch_reason': ev.get('switch_reason'),
        })
    return rows


def run_opening(state, turns, strategy=None):
    strategy = strategy or V1Strategy(BasicActionValidator())
    trail = []
    for _ in range(turns):
        commands = strategy.decide(state)
        trail.extend(ticks_from_state(state, commands))
        apply_opening_commands(state, commands)
        state.round_no += 1
        state.last_round_role_action_results = {k: True for k in commands}
    return trail


def format_trail(trail, limit=24):
    lines = ['round, worker_id, stage, position, goal_type, goal_pos, distance, action, switch_reason']
    for row in trail[:limit]:
        lines.append(
            '{round}, {worker_id}, {stage}, {position}, {goal_type}, {goal_pos}, '
            '{distance}, {action}, {switch_reason}'.format(**row)
        )
    return '\n'.join(lines)


def illegal_switches(trail):
    allowed = {
        None, 'sticky', 'nearest', 'stalled', 'sticky_gone', 'sticky_stalled',
        'backpack_full', 'gold_ready', 'have_voucher', 'muster', 'survival_wall',
        'build_weapons', 'wait_in_yard', 'blocked_by_nonstone_inventory', 'at_post',
        'stage_change',
    }
    return [row for row in trail if row.get('switch_reason') not in allowed]


def aba_oscillations(trail):
    """同阶段、同类采矿/墙目标之间的 A→B→A。合法阶段切换不计入。"""
    count = 0
    legal = {'muster', 'gold_ready', 'have_voucher', 'backpack_full'}
    for rid in (1, 2):
        rows = [row for row in trail if row['worker_id'] == rid]
        for i in range(2, len(rows)):
            a, b, c = rows[i - 2], rows[i - 1], rows[i]
            if a['stage'] != b['stage'] or b['stage'] != c['stage']:
                continue
            if a.get('switch_reason') in legal or c.get('switch_reason') in legal:
                continue
            if (c['position'] == a['position']
                    and c['position'] != b['position']
                    and c.get('goal_pos') and c.get('goal_pos') != a.get('goal_pos')):
                count += 1
    return count


class OpeningFsmTrailTests(unittest.TestCase):
    def test_scene_a_nearest_metal_until_upgrade_then_walls(self):
        state = opening_state()
        state.team_our.gold_num = 0
        _rockets(state)
        _map(state)
        for rid in (1, 2):
            next(r for r in state.team_our.roles if r.id == rid).back_pack_capability = 8
        trail = run_opening(state, 70)
        fund_rows = [r for r in trail if r['stage'] == STAGE_FUND]
        self.assertTrue(fund_rows)
        self.assertEqual(sum(1 for r in fund_rows if r['goal_type'] == 'stone'), 0)
        self.assertEqual(len(illegal_switches(trail)), 0)
        self.assertEqual(aba_oscillations(trail), 0)
        idle = [r for r in trail if r['worker_id'] in (1, 2) and not r['action']
                and r['stage'] in (STAGE_FUND, STAGE_WALL)]
        self.assertEqual(len(idle), 0)
        self.assertTrue(any(r['action'] in ('sell', 'buy', 'use') or r['goal_type'] in ('vendor', 'weaponShop', 'rocket')
                            for r in trail))
        print('\n=== 场景 A 轨迹 ===\n' + format_trail(trail))

    def test_scene_b_cutoff_switches_to_walls_once(self):
        state = opening_state()
        state.team_our.gold_num = 0
        _rockets(state)
        _map(state, quotes=True)
        for rid in (1, 2):
            w = next(r for r in state.team_our.roles if r.id == rid)
            w.back_pack_capability = 40
        trail = run_opening(state, 70)
        stages = []
        for row in trail:
            if row['worker_id'] == 1 and (not stages or stages[-1] != row['stage']):
                stages.append(row['stage'])
        self.assertIn(STAGE_FUND, stages)
        self.assertIn(STAGE_WALL, stages)
        self.assertNotIn(STAGE_FUND, stages[stages.index(STAGE_WALL):])
        fund_stone = sum(1 for r in trail if r['stage'] == STAGE_FUND and r['goal_type'] == 'stone')
        self.assertEqual(fund_stone, 0)
        wall_metal = [r for r in trail if r['stage'] == STAGE_WALL and r['goal_type'] in ('copper', 'iron')]
        self.assertEqual(wall_metal, [])
        self.assertTrue(any(r['stage'] == STAGE_WALL and r['goal_type'] in ('stone', 'wall', 'yard') for r in trail))
        self.assertTrue(any(r['stage'] == STAGE_MUSTER for r in trail))
        self.assertEqual(len(illegal_switches(trail)), 0)
        print('\n=== 场景 B 轨迹 ===\n' + format_trail(trail, 32))

    def test_scene_c_unreachable_nearest_keeps_goal(self):
        state = opening_state()
        state.round_no = 8
        state.team_our.gold_num = 0
        _rockets(state)
        state.map_info.zones = [
            Zone(Pos(0, 0), 'copper'),
            Zone(Pos(8, 9), 'iron'),
            Zone(Pos(6, 9), 'stone'),
            Zone(Pos(1, 11), 'vendor'),
        ]
        worker = state.team_our.roles[1]
        worker.pos = Pos(9, 9)
        trail = run_opening(state, 8)
        fund = [r for r in trail if r['worker_id'] == 1 and r['stage'] == STAGE_FUND]
        ores = {r['goal_type'] for r in fund if r['goal_type'] in ('copper', 'iron')}
        self.assertIn('iron', ores)
        self.assertEqual(aba_oscillations(fund), 0)

    def test_scene_d_both_workers_busy(self):
        state = opening_state()
        state.team_our.gold_num = 0
        _rockets(state)
        _map(state)
        trail = run_opening(state, 20)
        for row in trail:
            if row['stage'] in (STAGE_FUND, STAGE_WALL):
                self.assertTrue(row['action'], row)

    def test_budget_flags_follow_stage(self):
        state = opening_state()
        _rockets(state)
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        state.round_no = 8
        budget = opening_time_budget(state, missing, 62, 3, 0, False, build_blocked_set(state))
        self.assertEqual(budget['opening_stage'], STAGE_FUND)
        self.assertTrue(budget['allow_income_mine'])
        self.assertFalse(budget['allow_stone_mine'])
        self.assertFalse(budget['allow_walls'])
        state.policy_memory.clear()
        state.round_no = FIRST_UPGRADE_CUTOFF
        budget = opening_time_budget(state, missing, 30, 3, 0, False, build_blocked_set(state))
        self.assertEqual(budget['opening_stage'], STAGE_WALL)
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['allow_stone_mine'])

    def test_no_second_upgrade_on_day1(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 250
        _rockets(state, level=2)
        _map(state)
        V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(current_opening_stage(state), STAGE_WALL)
        self.assertFalse(any(c.get('action') == 'buy' for c in plan_opening(state).values()))
