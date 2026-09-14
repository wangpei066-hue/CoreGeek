"""首日阶段与夜间分工；合成地图不代表官方建造区已确认。"""
import unittest

from src.agent.brain import V1Strategy, BasicActionValidator, pick_weapon_name
from src.agent.opening import wall_ring, primary_wall_plan, assign_weapons, safe_wall, opening_time_budget
from src.agent.grid import build_blocked_set
from src.agent.protocol import Pos, Zone, RobotRole
from test_shop_items import minimal_state, make_role


def opening_state():
    state = minimal_state(round_no=0, gold_num=75)
    state.team_our.roles = [make_role(10, 10, 10, 'station', health=1500, level=1),
                            make_role(1, 9, 9, 'worker', health=220, back_pack_capability=100),
                            make_role(2, 12, 9, 'worker', health=220, back_pack_capability=100),
                            make_role(3, 10, 12, 'pioneer', health=200, back_pack_capability=40)]
    state.map_info.zones = [Zone(Pos(6, 9), 'stone')]
    return state


class OpeningTests(unittest.TestCase):
    def test_wanted_loadout_is_three_rockets(self):
        state = opening_state()
        self.assertEqual(pick_weapon_name(state), 'rocket')
        state.team_our.roles.append(make_role(20, 12, 10, 'rocket', level=1))
        self.assertEqual(pick_weapon_name(state), 'rocket')
        state.team_our.roles.append(make_role(21, 11, 10, 'rocket', level=1))
        self.assertEqual(pick_weapon_name(state), 'rocket')
        self.assertEqual(pick_weapon_name(state, ('rocket',)), 'rocket')

    def test_initial_workers_build_rockets_before_walls(self):
        state = opening_state()
        commands = V1Strategy(BasicActionValidator()).decide(state)
        for role_id in (1, 2):
            self.assertIn(commands[role_id]['action'], ('move', 'build'))
            if commands[role_id]['action'] == 'build':
                self.assertEqual(commands[role_id]['name'], 'rocket')
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == '武器' for e in state.decision_events))
        self.assertNotEqual(commands[1]['targetPos'], commands[2]['targetPos'])

    def test_wall_plan_faces_right_and_leaves_rear_open(self):
        state = opening_state()
        ring = wall_ring(state, state.team_our.roles[0])
        self.assertEqual(len(ring), 17)
        # 后方竖边保持开放；侧墙延伸到最靠后的短射程武器列。
        self.assertFalse(any(x == 9 and 7 < y < 12 for x, y in ring))
        self.assertTrue(all(x == 13 for x, y in ring[:6]))
        self.assertIn((13, 12), ring)

    def test_failed_wall_position_is_not_counted_as_completed(self):
        state = opening_state()
        state.round_no = 50
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        state.team_our.roles[2].backpack = ['stone'] * 4
        strategy = V1Strategy(BasicActionValidator())
        first = strategy.decide(state)
        state.round_no = 51
        state.last_round_role_action_results = {1: False, 2: False}
        second = strategy.decide(state)
        failed = {(c['targetPos'][0]['x'], c['targetPos'][0]['y'])
                  for c in first.values() if c['action'] == 'build'}
        self.assertTrue(all((c['targetPos'][0]['x'], c['targetPos'][0]['y']) not in failed
                            for c in second.values() if c['action'] == 'build'))
        phase = next(e for e in state.decision_events if e['code'] == 'opening_phase')
        self.assertEqual(phase['weapons'], 3)
        self.assertEqual(phase['phase'], '围墙')
        self.assertEqual(phase['walls_completed'], 0)
        self.assertTrue(failed)

    def test_late_day_skips_selling_to_finish_walls(self):
        state = opening_state()
        state.round_no = 52
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        worker = state.team_our.roles[1]
        worker.pos = Pos(8, 9)
        worker.backpack = ['copper'] * 26 + ['stone'] * 4
        state.map_info.zones.append(Zone(Pos(8, 9), 'vendor'))
        from src.agent.protocol import ShopItem
        state.vendor_shop_list = [ShopItem('copper', 5)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(c['action'] == 'sell' for c in commands.values()))
        self.assertTrue(any(e['code'] == 'opening_time_budget' and e['allow_sell'] is False
                            for e in state.decision_events))
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == '围墙' for e in state.decision_events))

    def test_enough_gold_buys_voucher_before_walls(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        worker = state.team_our.roles[1]
        worker.pos = Pos(8, 9)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c for c in commands.values() if c['action'] == 'buy']
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]['name'], 'WeaponUpgradeVoucher1')
        self.assertTrue(any(e['code'] == 'opening_time_budget' and e['allow_upgrade'] for e in state.decision_events))

    def test_time_budget_blocks_sell_when_walls_would_miss_night(self):
        state = opening_state()
        state.round_no = 55
        state.team_our.gold_num = 0
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 15, 3, 0, False, build_blocked_set(state))
        self.assertTrue(budget['allow_walls'])
        self.assertFalse(budget['allow_sell'])
        self.assertFalse(budget['allow_upgrade'])

    def test_gold_in_hand_still_buys_voucher_when_wall_time_is_tight(self):
        state = opening_state()
        state.round_no = 55
        state.team_our.gold_num = 130
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 15, 3, 130, False, build_blocked_set(state))
        self.assertTrue(budget['allow_upgrade'])
        self.assertFalse(budget['allow_sell'])
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].pos = Pos(8, 9)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[1]['action'], 'buy')
        self.assertEqual(commands[1]['name'], 'WeaponUpgradeVoucher1')

    def test_right_base_faces_left_after_switching_sides(self):
        state = opening_state()
        base = state.team_our.roles[0]
        base.pos = Pos(30, 8)
        line = wall_ring(state, base)
        self.assertEqual(len(line), 17)
        self.assertFalse(any(x == 32 and 5 < y < 10 for x, y in line))
        self.assertTrue(all(x == 28 for x, y in line[:6]))

    def test_cooldown_does_not_assign_same_weapon_twice(self):
        state = opening_state()
        state.round_no = 75
        state.team_our.roles += [make_role(20, 9, 10, 'rocket', cooldown=2, level=1),
                                 make_role(21, 12, 10, 'gatling', level=1),
                                 make_role(22, 10, 11, 'railgun', level=1)]
        V1Strategy(BasicActionValidator()).decide(state)
        allocations = [e['weapon_id'] for e in state.decision_events if e['code'] == 'weapon_assignment']
        self.assertEqual(len(allocations), 3)
        self.assertEqual(len(set(allocations)), 3)

    def test_pioneer_leaves_future_wall(self):
        state = opening_state()
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[3]['action'], 'move')
        target = commands[3]['targetPos'][0]
        self.assertNotIn((target['x'], target['y']), wall_ring(state, state.team_our.roles[0]))

    def test_night_three_different_weapons_and_high_tier_target(self):
        state = opening_state()
        state.round_no = 75
        for i, (kind, x) in enumerate([('gatling', 9), ('railgun', 12), ('rocket', 10)]):
            state.team_our.roles.append(make_role(20+i, x, 10 if i < 2 else 11, kind, level=1, attack_range=20))
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 1),
                             RobotRole(101, Pos(16, 10), 'bossRobot', 800)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        attacks = [c for c in commands.values() if c['action'] == 'attack']
        self.assertEqual(len(attacks), 3)
        self.assertEqual(len({c['controllerId'] for c in attacks}), 3)
        self.assertTrue(all(c['targetPos'] == [{'x': 16, 'y': 10}] for c in attacks))

    def test_complete_opening_on_synthetic_buildable_map(self):
        state = opening_state()
        strategy = V1Strategy(BasicActionValidator())
        wall_built = False
        for turn in range(70):
            state.round_no = turn
            commands = strategy.decide(state)
            results = {}
            for key, cmd in commands.items():
                role = next(r for r in state.team_our.roles if r.id == key)
                results[key] = True
                if cmd['action'] == 'move':
                    role.pos = Pos(**cmd['targetPos'][0])
                elif cmd['action'] == 'collect':
                    role.backpack.append('stone')
                elif cmd['action'] == 'build':
                    if cmd['name'] == 'wall':
                        self.assertGreaterEqual(sum(r.role_type == 'rocket' for r in state.team_our.roles), 3)
                        role.backpack.remove('stone')
                        wall_built = True
                    else:
                        self.assertFalse(wall_built)
                        self.assertEqual(cmd['name'], 'rocket')
                        state.team_our.gold_num -= 25
                    pos = cmd['targetPos'][0]
                    state.team_our.roles.append(make_role(100+len(state.team_our.roles), pos['x'], pos['y'], cmd['name'], level=1, attack_range=10))
            state.last_round_role_action_results = results
        self.assertTrue(wall_built)
        assignments = assign_weapons(state)
        self.assertEqual(len(assignments), 3)
        for role in state.team_our.roles:
            if role.id in assignments:
                weapon = assignments[role.id]
                self.assertLessEqual(max(abs(role.pos.x-weapon.pos.x), abs(role.pos.y-weapon.pos.y)), 1)
        kinds = [r.role_type for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        self.assertEqual(sorted(kinds), ['rocket', 'rocket', 'rocket'])
        xs = [r.pos.x for r in state.team_our.roles if r.role_type == 'rocket']
        self.assertEqual(len(set(xs)), 1)
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'}
        primary = set(primary_wall_plan(state, state.team_our.roles[0]))
        self.assertTrue(walls <= primary)
        self.assertGreaterEqual(len(walls), 7)
        self.assertLessEqual(len(walls), 8)


    def _rockets(self, state):
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]

    def test_worker_buys_voucher_not_pioneer(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].pos = Pos(8, 9)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[1]['action'], 'buy')
        self.assertEqual(commands[1]['name'], 'WeaponUpgradeVoucher1')
        self.assertNotIn(3, state.worker_item_jobs)

    def test_outside_worker_enters_courtyard_instead_of_patrolling(self):
        state = opening_state()
        state.round_no = 45
        state.team_our.gold_num = 0
        self._rockets(state)
        worker = state.team_our.roles[1]
        worker.pos = Pos(14, 10)
        worker.backpack = ['stone'] * 6
        state.team_our.roles[2].backpack = ['stone'] * 6
        strategy = V1Strategy(BasicActionValidator())
        xs = []
        built = 0
        for turn in range(8):
            state.round_no = 45 + turn
            commands = strategy.decide(state)
            cmd = commands.get(1, {})
            if cmd.get('action') == 'move':
                worker.pos = Pos(**cmd['targetPos'][0])
            elif cmd.get('action') == 'build' and cmd.get('name') == 'wall':
                pos = cmd['targetPos'][0]
                state.team_our.roles.append(make_role(80 + turn, pos['x'], pos['y'], 'wall', level=1))
                worker.backpack.remove('stone')
                built += 1
            xs.append(worker.pos.x)
        self.assertTrue(built or min(xs) < 14, xs)
        self.assertFalse(all(x == 14 for x in xs), xs)

    def test_gold_and_walls_progress_instead_of_idling(self):
        from src.agent.protocol import ShopItem
        state = opening_state()
        state.team_our.gold_num = 130
        self._rockets(state)
        state.map_info.zones = [Zone(Pos(6, 9), 'stone'), Zone(Pos(1, 9), 'weaponShop'), Zone(Pos(1, 11), 'vendor')]
        state.weapon_shop_list = [ShopItem('WeaponUpgradeVoucher1', 100)]
        state.team_our.roles[1].backpack = ['stone'] * 4
        state.team_our.roles[2].backpack = ['stone'] * 4
        strategy = V1Strategy(BasicActionValidator())
        idle_moves = {1: [], 2: []}
        for turn in range(16):
            state.round_no = 35 + turn
            commands = strategy.decide(state)
            for rid in (1, 2):
                role = next(r for r in state.team_our.roles if r.id == rid)
                cmd = commands.get(rid, {})
                idle_moves[rid].append((role.pos.x, role.pos.y))
                if cmd.get('action') == 'move':
                    role.pos = Pos(**cmd['targetPos'][0])
                elif cmd.get('action') == 'build' and cmd.get('name') == 'wall':
                    pos = cmd['targetPos'][0]
                    role.backpack.remove('stone')
                    state.team_our.roles.append(make_role(90 + len(state.team_our.roles), pos['x'], pos['y'], 'wall', level=1))
                elif cmd.get('action') == 'buy':
                    role.backpack.append(cmd['name'])
                    state.team_our.gold_num -= 100
                elif cmd.get('action') == 'use' and cmd.get('name') in role.backpack:
                    role.backpack.remove(cmd['name'])
                    tp = cmd['targetPos'][0]
                    for item in state.team_our.roles:
                        if item.pos.x == tp['x'] and item.pos.y == tp['y'] and item.role_type == 'rocket':
                            item.level = (item.level or 1) + 1
        walls = sum(r.role_type == 'wall' for r in state.team_our.roles)
        upgraded = any((r.level or 1) >= 2 for r in state.team_our.roles if r.role_type == 'rocket')
        has_voucher = any('WeaponUpgradeVoucher1' in r.backpack for r in state.team_our.roles)
        self.assertGreaterEqual(walls, 4)
        self.assertTrue(state.team_our.gold_num < 130 or upgraded or has_voucher)
        self.assertTrue(upgraded or has_voucher or any(
            job.get('kind') == 'weapon' for job in state.worker_item_jobs.values()))
        for rid, path in idle_moves.items():
            self.assertGreater(len(set(path)), 1)
            cycle = path[-6:]
            self.assertFalse(len(set(cycle)) <= 2 and len(set(path)) <= 3,
                             'worker %s appears to patrol a tiny loop: %s' % (rid, path))

    def test_full_backpack_drops_ore_then_buys_voucher(self):
        from src.agent.protocol import ShopItem
        state = opening_state()
        state.round_no = 30
        state.team_our.gold_num = 130
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.weapon_shop_list = [ShopItem('WeaponUpgradeVoucher1', 100)]
        worker = state.team_our.roles[1]
        worker.pos = Pos(8, 9)
        worker.back_pack_capability = 4
        worker.backpack = ['stone'] * 4
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[1]['action'], 'drop')
        self.assertEqual(commands[1]['name'], 'stone')

    def test_inner_fighters_take_inner_guns_outer_takes_flank(self):
        state = opening_state()
        state.round_no = 75
        self._rockets(state)
        for rocket in state.team_our.roles[-3:]:
            rocket.attack_range = 20
        inner = next(r for r in state.team_our.roles if r.id == 20)
        state.team_our.roles[1].pos = Pos(11, 10)
        state.team_our.roles[2].pos = Pos(11, 12)
        state.team_our.roles[3].pos = Pos(14, 10)
        state.robot.roles = [RobotRole(100, Pos(16, 10), 'bossRobot', 800)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        assigned = {e['role_id']: e['weapon_id'] for e in state.decision_events if e['code'] == 'weapon_assignment'}
        self.assertEqual(assigned[1], inner.id)
        self.assertNotEqual(assigned[3], inner.id)
        self.assertEqual(len(set(assigned.values())), 3)
        attacks = [c for c in commands.values() if c['action'] == 'attack']
        movers = [rid for rid, c in commands.items() if c['action'] == 'move' and rid in (1, 2, 3)]
        self.assertEqual(len(attacks) + len(movers), 3)
        self.assertIn(1, {int(c['controllerId']) for c in attacks})
        if 3 in movers:
            step = commands[3]['targetPos'][0]
            self.assertNotEqual((step['x'], step['y']), (state.team_our.roles[1].pos.x, state.team_our.roles[1].pos.y))

