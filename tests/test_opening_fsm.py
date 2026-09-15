"""第一天状态机：连续回合轨迹，而不是单快照。"""
import unittest

from src.agent.brain import V1Strategy, BasicActionValidator
from src.agent.opening import opening_time_budget, primary_wall_plan, plan_opening
from src.agent.opening_schedule import (
    FIRST_UPGRADE_CUTOFF, STAGE_APPLY, STAGE_FUND, STAGE_MUSTER, STAGE_WALL,
    current_opening_stage,
)
from src.agent.grid import build_blocked_set
from src.agent.protocol import Pos, Zone, ShopItem, PlayerTask
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
        'stage_change', 'batch_not_ready', 'batch_ready', 'mine_exhausted',
        'wall_stage_metal_unused', 'muster_cashout_before_night', 'urgent_wall',
        'clearly_closer', 'nearest_relaxed_reserved', 'sticky_relaxed_reserved',
        'clearly_closer_relaxed_reserved', 'stalled_relaxed_reserved',
        'sticky_gone_relaxed_reserved', 'sticky_stalled_relaxed_reserved',
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
    def test_scene_a_walls_before_day1_upgrade(self):
        state = opening_state()
        state.team_our.gold_num = 0
        _rockets(state)
        _map(state)
        for rid in (1, 2):
            next(r for r in state.team_our.roles if r.id == rid).back_pack_capability = 8
        trail = run_opening(state, 70)
        fund_rows = [r for r in trail if r['stage'] == STAGE_FUND]
        self.assertEqual(fund_rows, [])
        wall_rows = [r for r in trail if r['stage'] == STAGE_WALL]
        self.assertTrue(wall_rows)
        self.assertEqual([r for r in wall_rows if r['goal_type'] in ('copper', 'iron')], [])
        self.assertEqual(len(illegal_switches(trail)), 0)
        self.assertTrue(any(r.get('switch_reason') == 'batch_not_ready' for r in wall_rows))
        self.assertTrue(any(r.get('switch_reason') == 'batch_ready' for r in wall_rows))
        idle = [r for r in trail if r['worker_id'] in (1, 2) and not r['action']
                and r['stage'] in (STAGE_FUND, STAGE_WALL)]
        self.assertEqual(len(idle), 0)
        walls = [r for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0]
        self.assertGreaterEqual(len(walls), 7)
        print('\n=== 场景 A 轨迹 ===\n' + format_trail(trail))

    def test_scene_b_goes_to_walls_once_after_weapons(self):
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
        self.assertIn(STAGE_WALL, stages)
        self.assertNotIn(STAGE_FUND, stages)
        wall_metal = [r for r in trail if r['stage'] == STAGE_WALL and r['goal_type'] in ('copper', 'iron')]
        self.assertEqual(wall_metal, [])
        self.assertTrue(any(r['stage'] == STAGE_WALL and r['goal_type'] in ('stone', 'wall', 'yard') for r in trail))
        self.assertTrue(any(r['stage'] == STAGE_MUSTER for r in trail))
        self.assertEqual(len(illegal_switches(trail)), 0)
        print('\n=== 场景 B 轨迹 ===\n' + format_trail(trail, 32))

    def test_scene_c_day1_prefers_stone_over_reachable_metal(self):
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
        wall = [r for r in trail if r['worker_id'] == 1 and r['stage'] == STAGE_WALL]
        ores = {r['goal_type'] for r in wall if r['goal_type'] in ('stone', 'copper', 'iron')}
        self.assertIn('stone', ores)
        self.assertNotIn('copper', ores)
        self.assertNotIn('iron', ores)
        self.assertEqual(aba_oscillations(wall), 0)

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
        self.assertEqual(budget['opening_stage'], STAGE_WALL)
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['allow_stone_mine'])
        self.assertTrue(budget['allow_walls'])
        state.policy_memory.clear()
        state.round_no = FIRST_UPGRADE_CUTOFF
        budget = opening_time_budget(state, missing, 30, 3, 0, False, build_blocked_set(state))
        self.assertEqual(budget['opening_stage'], STAGE_WALL)
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['allow_stone_mine'])

    def test_weapons_finished_after_cutoff_does_not_stick_in_build_weapons(self):
        """回归：三炮在筹资截止点之后才建完时，阶段必须能从 BUILD_WEAPONS 直接跳到
        BUILD_SURVIVAL_WALL，不能因为 LEGAL_TRANSITIONS 缺一条边而卡死在原地空转。"""
        state = opening_state()
        state.round_no = FIRST_UPGRADE_CUTOFF - 2
        state.team_our.gold_num = 75
        _map(state, quotes=False)
        trail = run_opening(state, 40)
        stages = [row['stage'] for row in trail if row['worker_id'] == 1]
        self.assertIn(STAGE_WALL, stages)
        self.assertNotIn('BUILD_WEAPONS', stages[stages.index(STAGE_WALL):])
        no_command = [e for e in state.decision_events
                      if e.get('code') == 'worker_no_command' and e.get('worker_state') == 'BUILD_WEAPONS']
        self.assertEqual(no_command, [])

    def test_stone_batches_before_building_walls_while_time_allows(self):
        """第一天生存墙优先，但白天还够时先攒一批石头，避免一块一跑。"""
        state = opening_state()
        state.round_no = 45
        state.team_our.gold_num = 0
        _rockets(state)
        state.map_info.zones = [
            Zone(Pos(6, 9), 'stone'), Zone(Pos(1, 9), 'weaponShop'), Zone(Pos(1, 11), 'vendor'),
        ]
        trail = run_opening(state, 40)
        wall_rows = [r for r in trail if r['worker_id'] == 1 and r['stage'] == STAGE_WALL]
        builds = [r for r in wall_rows if r['action'] == 'build' and r['goal_type'] == 'wall']
        self.assertGreater(len(builds), 0)
        self.assertTrue(any(r.get('switch_reason') == 'batch_not_ready' for r in wall_rows))
        self.assertTrue(any(r.get('switch_reason') == 'batch_ready' for r in wall_rows))

    def test_opening_wall_work_batches_one_stone_until_late(self):
        """opening_wall_work 单测：早期 1 石继续采，临近入夜才提前补墙。"""
        from src.agent.opening_schedule import opening_wall_work
        from src.agent.opening import movement_avoid
        state = opening_state()
        state.round_no = 45
        state.team_our.gold_num = 0
        _rockets(state)
        state.map_info.zones = [Zone(Pos(6, 9), 'stone')]
        worker = next(r for r in state.team_our.roles if r.id == 1)
        blocked = build_blocked_set(state) | movement_avoid(state)

        worker.backpack = ['stone']
        state.decision_events = []
        opening_wall_work(worker, state, blocked, set(), set(), {})
        tick = next(e for e in state.decision_events if e['code'] == 'opening_worker_tick')
        self.assertEqual(tick.get('goal_type'), 'stone')
        self.assertEqual(tick.get('switch_reason'), 'batch_not_ready')

        state.round_no = 65
        worker.pos = Pos(12, 7)
        blocked = build_blocked_set(state) | movement_avoid(state)
        state.decision_events = []
        opening_wall_work(worker, state, blocked, set(), set(), {})
        late_tick = next(e for e in state.decision_events if e['code'] == 'opening_worker_tick')
        self.assertEqual(late_tick.get('goal_type'), 'wall')
        self.assertEqual(late_tick.get('switch_reason'), 'urgent_wall')

        worker.backpack = ['stone'] * 6
        state.round_no = 45
        state.decision_events = []
        opening_wall_work(worker, state, blocked, set(), set(), {})
        tick2 = next(e for e in state.decision_events if e['code'] == 'opening_worker_tick')
        self.assertEqual(tick2.get('goal_type'), 'wall')
        self.assertEqual(tick2.get('switch_reason'), 'batch_ready')

    def test_claimed_mine_does_not_force_large_detour(self):
        from src.agent.opening_schedule import choose_nearest_mine
        from src.agent.opening import movement_avoid
        state = opening_state()
        state.round_no = 45
        state.team_our.gold_num = 0
        _rockets(state)
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(7, 9)
        state.map_info.zones = [Zone(Pos(6, 9), 'stone'), Zone(Pos(20, 20), 'stone')]
        state.policy_memory['mine_targets'] = {'2': {'x': 6, 'y': 9, 'ore': 'stone'}}
        blocked = build_blocked_set(state) | movement_avoid(state)
        mine, path, reason = choose_nearest_mine(worker, state, blocked, set(), ('stone',))
        self.assertEqual((mine.pos.x, mine.pos.y), (6, 9))
        self.assertEqual(path, [])

    def test_clearly_closer_mine_breaks_sticky_goal(self):
        from src.agent.opening_schedule import choose_nearest_mine
        from src.agent.opening import movement_avoid
        state = opening_state()
        state.round_no = 45
        state.team_our.gold_num = 0
        _rockets(state)
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(7, 9)
        state.map_info.zones = [Zone(Pos(6, 9), 'stone'), Zone(Pos(20, 20), 'stone')]
        state.policy_memory['opening_worker_goals'] = {
            '1': {
                'stage': STAGE_WALL, 'kind': 'mine', 'target_type': 'stone',
                'target_pos': [20, 20], 'stalled_rounds': 0, 'last_pos': [7, 9],
            }
        }
        blocked = build_blocked_set(state) | movement_avoid(state)
        mine, path, reason = choose_nearest_mine(worker, state, blocked, set(), ('stone',))
        self.assertEqual((mine.pos.x, mine.pos.y), (6, 9))
        self.assertEqual(reason, 'clearly_closer')

    def test_wall_stage_sells_metal_even_when_backpack_not_full(self):
        """回归：修墙阶段背包没满也不能一直攥着铜铁不出手，浪费到入夜。"""
        from src.agent.opening_schedule import opening_wall_work
        from src.agent.opening import movement_avoid
        state = opening_state()
        state.round_no = 45
        state.team_our.gold_num = 0
        _rockets(state)
        state.map_info.zones = [
            Zone(Pos(6, 9), 'stone'), Zone(Pos(1, 11), 'vendor'),
        ]
        state.vendor_shop_list = [ShopItem('iron', 3)]
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.backpack = ['iron'] * 10  # 远没塞满 100 容量的背包
        blocked = build_blocked_set(state) | movement_avoid(state)
        state.decision_events = []
        opening_wall_work(worker, state, blocked, set(), set(), {})
        tick = next(e for e in state.decision_events if e['code'] == 'opening_worker_tick')
        self.assertEqual(tick.get('goal_type'), 'vendor')
        self.assertEqual(tick.get('switch_reason'), 'wall_stage_metal_unused')

    def test_role_sells_metal_before_early_muster_when_time_allows(self):
        """回归：到个人回防点时背包还有铜铁、且时间够，要先绕去卖掉再回炮，不能直接空转带进夜里。"""
        state = opening_state()
        state.round_no = 60
        state.team_our.gold_num = 0
        _rockets(state)
        state.map_info.zones = [Zone(Pos(1, 11), 'vendor')]
        state.vendor_shop_list = [ShopItem('iron', 3)]
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.backpack = ['iron'] * 10
        state.policy_memory['opening_stage'] = STAGE_WALL
        commands = V1Strategy(BasicActionValidator()).decide(state)
        events = [e for e in state.decision_events
                  if e.get('code') == 'muster_cashout_before_night' and e.get('role_id') == worker.id]
        self.assertTrue(events)
        self.assertEqual(commands.get(worker.id, {}).get('action'), 'move')

    def test_pioneer_busy_does_not_block_day1_wall_work(self):
        """首日三炮齐后先修墙；开拓者忙任务也不能让工人卡在买券选择上。"""
        state = opening_state()
        state.team_our.gold_num = 200
        _rockets(state)
        state.map_info.zones = [
            Zone(Pos(1, 9), 'weaponShop'), Zone(Pos(6, 9), 'stone'), Zone(Pos(1, 11), 'vendor'),
        ]
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 5), ShopItem('stone', 1)]
        state.weapon_shop_list = [ShopItem('WeaponUpgradeVoucher1', 100)]
        pioneer = next(r for r in state.team_our.roles if r.id == 3)
        pioneer.pos = Pos(2, 9)  # 离商店比两名工人都近，是 _voucher_buyer_id 天然会选中的对象
        state.phase_task = '部署修复任务：工作区为 /srv/app/'
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(2, 9), 0, 10, 10, True)]

        trail = run_opening(state, 15)  # trail 只记录工人(1,2)，天然排除开拓者
        bought_by_worker = any(row['action'] == 'buy' for row in trail)
        self.assertFalse(bought_by_worker, format_trail(trail))
        self.assertTrue(any(row['stage'] == STAGE_WALL and row['goal_type'] in ('stone', 'wall')
                            for row in trail), format_trail(trail))

    def test_no_second_upgrade_on_day1(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 250
        _rockets(state, level=2)
        _map(state)
        V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(current_opening_stage(state), STAGE_WALL)
        self.assertFalse(any(c.get('action') == 'buy' for c in plan_opening(state).values()))
