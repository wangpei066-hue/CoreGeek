"""首日阶段与夜间分工；合成地图不代表官方建造区已确认。"""
import unittest

from src.agent.brain import V1Strategy, BasicActionValidator, pick_weapon_name
from src.agent.opening import (
    wall_ring, primary_wall_plan, assign_weapons, safe_wall, opening_time_budget,
    estimate_opening_upgrade, day_rounds_remaining, plan_opening,
)
from src.agent.grid import build_blocked_set
from src.agent.protocol import Pos, Zone, RobotRole, ShopItem
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
    def test_wanted_loadout_is_two_rockets_one_railgun(self):
        state = opening_state()
        self.assertEqual(pick_weapon_name(state), 'rocket')
        state.team_our.roles.append(make_role(20, 12, 10, 'rocket', level=1))
        self.assertEqual(pick_weapon_name(state), 'rocket')
        state.team_our.roles.append(make_role(21, 11, 10, 'rocket', level=1))
        self.assertEqual(pick_weapon_name(state), 'railgun')
        # extra_names 表示本回合已规划的建造，视同已占用该槽位，不再重复要电磁炮。
        self.assertEqual(pick_weapon_name(state, ('railgun',)), 'rocket')
        state.team_our.roles.append(make_role(22, 13, 10, 'railgun', level=1))
        self.assertEqual(pick_weapon_name(state), 'rocket')

    def test_upgrade_prefers_rocket_over_railgun(self):
        from src.agent.brain import _pick_upgradeable, WEAPON_TYPES
        state = opening_state()
        rocket = make_role(20, 12, 10, 'rocket', level=1, health=1000)
        railgun = make_role(21, 12, 8, 'railgun', level=1, health=100)
        state.team_our.roles += [rocket, railgun]
        self.assertEqual(_pick_upgradeable(state, WEAPON_TYPES, set()).role_type, 'rocket')

    def test_initial_workers_build_rockets_before_walls(self):
        state = opening_state()
        commands = V1Strategy(BasicActionValidator()).decide(state)
        for role_id in (1, 2):
            self.assertIn(commands[role_id]['action'], ('move', 'build'))
            if commands[role_id]['action'] == 'build':
                self.assertEqual(commands[role_id]['name'], 'rocket')
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == '武器' for e in state.decision_events))
        phase = next(e for e in state.decision_events if e['code'] == 'opening_phase')
        self.assertEqual(phase['cycle'], 0)
        self.assertEqual(phase['rounds_to_night'], 70)
        self.assertEqual(phase['alive_weapons'], 0)
        self.assertNotEqual(commands[1]['targetPos'], commands[2]['targetPos'])

    def test_wall_plan_faces_right_and_leaves_rear_open(self):
        state = opening_state()
        ring = wall_ring(state, state.team_our.roles[0])
        self.assertEqual(len(ring), 14)  # 单层：正面一列 + 两侧翼，无外层
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
        state.team_our.roles[2].backpack = ['stone'] * 6  # 攒够一批(STONE_BATCH)才会立即去建墙
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
        self.assertEqual(phase['phase'], 'SURVIVAL_WALL')
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
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] in ('围墙', 'SURVIVAL_WALL')
                            for e in state.decision_events))

    def test_enough_gold_pioneer_buys_voucher_while_workers_build_walls(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c for c in commands.values() if c['action'] == 'buy']
        self.assertEqual(len(buys), 1)
        self.assertEqual(commands.get(pioneer.id, {}).get('action'), 'buy')
        self.assertEqual(commands.get(pioneer.id, {}).get('name'), 'WeaponUpgradeVoucher1')
        self.assertNotIn(1, state.worker_item_jobs)
        self.assertTrue(any(e['code'] == 'opening_time_budget' and e['allow_walls'] for e in state.decision_events))
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == 'SURVIVAL_WALL'
                            for e in state.decision_events))

    def test_two_weapons_on_rear_rank_one_cell_forward(self):
        from src.agent.opening import weapon_slots
        state = opening_state()
        slots = weapon_slots(state, state.team_our.roles[0])
        self.assertEqual(len(slots), 3)
        self.assertEqual(slots[0][0], slots[1][0])
        self.assertEqual(slots[2][0], slots[0][0] + 1)
        self.assertNotEqual(slots[0][1], slots[1][1])

    def test_time_budget_blocks_sell_when_walls_would_miss_night(self):
        state = opening_state()
        state.round_no = 55
        state.team_our.gold_num = 0
        self._rockets(state)
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 15, 3, 0, False, build_blocked_set(state))
        self.assertTrue(budget['allow_walls'])
        self.assertFalse(budget['allow_sell'])
        self.assertFalse(budget['allow_upgrade'])

    def test_day1_mines_stone_before_upgrade(self):
        from src.agent.protocol import ShopItem
        state = opening_state()
        state.round_no = 8
        state.team_our.gold_num = 0
        self._rockets(state)
        state.map_info.zones = [
            Zone(Pos(6, 9), 'stone'),
            Zone(Pos(9, 6), 'copper'),
            Zone(Pos(12, 6), 'iron'),
            Zone(Pos(1, 9), 'weaponShop'),
            Zone(Pos(1, 11), 'vendor'),
        ]
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 3), ShopItem('stone', 1)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        ores = {
            (state.policy_memory.get('mine_targets') or {}).get(str(rid), {}).get('ore')
            for rid in (1, 2)
        }
        ores.discard(None)
        self.assertTrue(ores)
        self.assertIn('stone', ores)
        self.assertIn('copper', ores)
        self.assertEqual(state.policy_memory.get('opening_stage'), 'BUILD_SURVIVAL_WALL')
        for rid in (1, 2):
            cmd = commands.get(rid) or {}
            self.assertIn(cmd.get('action'), ('move', 'collect'))

    def test_day1_mines_stone_after_first_weapon_is_level_two(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        rockets = [r for r in state.team_our.roles if r.role_type == 'rocket']
        rockets[0].level = 2
        budget = opening_time_budget(
            state, primary_wall_plan(state, state.team_our.roles[0])[:8],
            50, 3, 0, 1, build_blocked_set(state),
        )
        self.assertTrue(budget['allow_walls'])
        self.assertTrue(budget['required_done'])
        self.assertFalse(budget['allow_mine'])
        self.assertFalse(budget['allow_sell'])
        state.map_info.zones = [
            Zone(Pos(6, 9), 'stone'),
            Zone(Pos(9, 6), 'copper'),
            Zone(Pos(1, 9), 'weaponShop'),
            Zone(Pos(1, 11), 'vendor'),
        ]
        V1Strategy(BasicActionValidator()).decide(state)
        ores = {
            (state.policy_memory.get('mine_targets') or {}).get(str(rid), {}).get('ore')
            for rid in (1, 2)
        }
        self.assertIn('stone', ores)
        self.assertIn('copper', ores)
        self.assertNotIn('iron', ores)

    def test_day1_pioneer_can_buy_upgrade_while_workers_keep_walling(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 250
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        pioneer.pos = Pos(8, 9)
        worker.pos = Pos(8, 10)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c for c in commands.values() if c.get('action') == 'buy'
                and c.get('name') == 'WeaponUpgradeVoucher1']
        self.assertEqual(len(buys), 1)
        self.assertEqual(commands.get(pioneer.id, {}).get('action'), 'buy')
        self.assertFalse(any(j.get('kind') == 'weapon' for j in state.worker_item_jobs.values()))

    def test_day1_second_upgrade_runs_in_parallel_after_first_l2(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 250
        self._rockets(state)
        next(r for r in state.team_our.roles if r.role_type == 'rocket').level = 2
        state.map_info.zones += [Zone(Pos(8, 9), 'weaponShop'), Zone(Pos(6, 9), 'stone')]
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        other = next(r for r in state.team_our.roles if r.role_type == 'worker' and r.id != worker.id)
        worker.pos = Pos(8, 9)
        other.backpack = ['stone'] * 4
        budget = opening_time_budget(
            state, primary_wall_plan(state, state.team_our.roles[0])[:8],
            50, 3, 250, 1, build_blocked_set(state),
        )
        self.assertTrue(budget['allow_walls'])
        self.assertFalse(budget['allow_upgrade'])
        self.assertFalse(budget['allow_mine'])
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c for c in commands.values() if c.get('action') == 'buy'
                and c.get('name') == 'WeaponUpgradeVoucher1']
        self.assertEqual(len(buys), 0)
        self.assertTrue(any(c.get('action') in ('move', 'build', 'collect') for rid, c in commands.items()
                            if rid == other.id))

    def test_gold_in_hand_still_prioritizes_walls_when_wall_time_is_tight(self):
        state = opening_state()
        state.round_no = 55
        state.team_our.gold_num = 130
        self._rockets(state)
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 15, 3, 130, False, build_blocked_set(state))
        self.assertFalse(budget['allow_upgrade'])
        self.assertFalse(budget['allow_sell'])
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands.get(pioneer.id, {}).get('action'), 'buy')
        self.assertEqual(commands.get(pioneer.id, {}).get('name'), 'WeaponUpgradeVoucher1')

    def test_right_base_faces_left_after_switching_sides(self):
        state = opening_state()
        base = state.team_our.roles[0]
        base.pos = Pos(30, 8)
        line = wall_ring(state, base)
        self.assertEqual(len(line), 14)
        self.assertFalse(any(x == 32 and 5 < y < 10 for x, y in line))
        self.assertTrue(all(x == 28 for x, y in line[:6]))

    def test_cooldown_does_not_assign_same_weapon_twice(self):
        state = opening_state()
        state.round_no = 80
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
                        self.assertGreaterEqual(
                            sum(r.role_type in ('rocket', 'railgun') for r in state.team_our.roles), 3)
                        role.backpack.remove('stone')
                        wall_built = True
                    else:
                        self.assertFalse(wall_built)
                        self.assertIn(cmd['name'], ('rocket', 'railgun'))
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
                self.assertLessEqual(max(abs(role.pos.x-weapon.pos.x), abs(role.pos.y-weapon.pos.y)), 2)
        kinds = [r.role_type for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        self.assertEqual(sorted(kinds), ['railgun', 'rocket', 'rocket'])
        xs = [r.pos.x for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        self.assertEqual(len(set(xs)), 2)
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'}
        primary = set(primary_wall_plan(state, state.team_our.roles[0]))
        self.assertTrue(walls <= primary)
        # 攒够 STONE_BATCH(6) 再成片建墙后，同样的 70 回合窗口里完工数会比"采一块建一道"更少，
        # 这是批量搬运减少往返的预期代价，不是回归；只要求确实有墙建成。
        self.assertGreaterEqual(len(walls), 8)


    def _rockets(self, state):
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]

    def test_worker_near_shop_does_not_buy_voucher_before_day1_walls(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        for i, y in enumerate(range(7, 13)):
            state.team_our.roles.append(make_role(40 + i, 13, y, 'wall', level=1, health=1000))
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].pos = Pos(8, 9)
        state.policy_memory['weapon_assignment'] = {'1': 20, '2': 21, '3': 22}
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(c.get('action') == 'buy' for c in commands.values()))
        self.assertIn(commands[1]['action'], ('move', 'collect', 'build'))
        self.assertNotEqual(commands.get(3, {}).get('name'), 'WeaponUpgradeVoucher1')

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
        idle_moves = {1: [], 2: [], 3: []}
        actions = {1: [], 2: [], 3: []}
        for turn in range(16):
            state.round_no = 35 + turn
            commands = strategy.decide(state)
            for rid in (1, 2, 3):
                role = next(r for r in state.team_our.roles if r.id == rid)
                cmd = commands.get(rid, {})
                idle_moves[rid].append((role.pos.x, role.pos.y))
                actions[rid].append(cmd.get('action'))
                if cmd.get('action') == 'move':
                    role.pos = Pos(**cmd['targetPos'][0])
                elif cmd.get('action') == 'collect':
                    role.backpack.append('stone')
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
        self.assertTrue(walls >= 1 or upgraded or has_voucher or state.team_our.gold_num < 130)
        for rid in (1, 2):
            path = idle_moves[rid]
            self.assertGreater(len(set(path)), 1)
            cycle = path[-6:]
            if len(set(cycle)) <= 2 and len(set(path)) <= 3:
                self.assertTrue(all(a == 'collect' for a in actions[rid][-6:]),
                                'worker %s appears to patrol a tiny loop: %s' % (rid, path))

    def test_full_backpack_stone_is_not_dropped_for_voucher_before_day1_walls(self):
        from src.agent.protocol import ShopItem
        state = opening_state()
        state.round_no = 30
        state.team_our.gold_num = 130
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.weapon_shop_list = [ShopItem('WeaponUpgradeVoucher1', 100)]
        worker = state.team_our.roles[1]
        worker.pos = Pos(9, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        pioneer.back_pack_capability = 4
        pioneer.backpack = ['stone'] * 4
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'drop')
        self.assertFalse(any(c.get('name') == 'WeaponUpgradeVoucher1' for c in commands.values()))

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

    def test_night_cleared_wave_sends_units_to_work(self):
        from src.agent.protocol import PlayerTask
        state = opening_state()
        state.round_no = 80
        self._rockets(state)
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles[1].backpack = ['stone'] * 4
        state.team_our.roles[2].backpack = ['stone'] * 4
        state.robot.roles = []
        state.policy_memory['night_saw_threat'] = True
        state.policy_memory['night_empty_streak'] = 7
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(e['code'] == 'night_wave_cleared' for e in state.decision_events))
        self.assertFalse(any(c['action'] == 'attack' for c in commands.values()))
        self.assertTrue(any(c['action'] in ('move', 'collect', 'build', 'acceptTask') for c in commands.values()))
        self.assertIn(commands[3]['action'], ('move', 'acceptTask', 'collect'))

    def test_night_probe_revoked_when_robots_return(self):
        state = opening_state()
        state.round_no = 81
        self._rockets(state)
        state.team_our.roles[1].pos = Pos(6, 9)
        state.team_our.roles[1].backpack = []
        state.team_our.roles[2].backpack = []
        state.policy_memory['night_saw_threat'] = True
        state.policy_memory['night_empty_streak'] = 8
        state.robot.roles = [RobotRole(30001, Pos(20, 10), 'smallRobot', 40)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(state.policy_memory['night_empty_streak'], 0)
        self.assertFalse(any(e['code'] == 'night_wave_cleared' for e in state.decision_events))
        self.assertFalse(any(c['action'] in ('collect', 'acceptTask') for c in commands.values()))
        self.assertTrue(any(c['action'] in ('attack', 'move') for c in commands.values()))

    def test_front_gap_with_stone_pauses_voucher_buy(self):
        from src.agent.protocol import PlayerTask
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.phase_task = {'taskType': '自进化类1'}
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles[1].pos = Pos(12, 7)
        state.team_our.roles[1].backpack = ['stone'] * 2
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertIn(commands[1]['action'], ('move', 'buy', 'build'))
        self.assertEqual((state.policy_memory.get('mine_targets') or {}).get('1', {}).get('ore'), 'stone')

    def test_night_empty_one_round_still_holds_guns(self):
        state = opening_state()
        state.round_no = 80
        self._rockets(state)
        state.team_our.roles[1].backpack = ['stone'] * 4
        state.team_our.roles[2].backpack = ['stone'] * 4
        state.robot.roles = []
        state.policy_memory['night_saw_threat'] = True
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(e['code'] == 'night_wave_cleared' for e in state.decision_events))
        self.assertFalse(any(c['action'] in ('collect', 'acceptTask') for c in commands.values()))

    def test_night_local_streak_builds_adjacent_front_wall(self):
        from src.agent.tactics import WAVE_LOCAL_STREAK
        state = opening_state()
        state.round_no = 80
        state.team_our.roles += [
            make_role(20, 11, 10, 'rocket', level=2, attack_range=20),
            make_role(21, 9, 8, 'rocket', level=2, attack_range=20),
            make_role(22, 9, 11, 'rocket', level=2, attack_range=20),
        ]
        state.team_our.roles[1].pos = Pos(12, 10)
        state.team_our.roles[1].backpack = ['stone'] * 4
        state.team_our.roles[2].pos = Pos(9, 8)
        state.team_our.roles[3].pos = Pos(9, 11)
        state.policy_memory['weapon_assignment'] = {'1': 20, '2': 21, '3': 22}
        state.robot.roles = []
        state.policy_memory['night_saw_threat'] = True
        state.policy_memory['night_empty_streak'] = WAVE_LOCAL_STREAK - 1
        commands = V1Strategy(BasicActionValidator()).decide(state)
        builds = [c for c in commands.values() if c.get('action') == 'build' and c.get('name') == 'wall']
        self.assertTrue(builds)
        target = builds[0]['targetPos'][0]
        self.assertEqual(target['x'], 13)
        self.assertEqual(max(abs(target['x'] - 12), abs(target['y'] - 10)), 1)


class OpeningUpgradeEstimateTests(unittest.TestCase):
    def _rockets(self, state):
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1, health=1000),
            make_role(21, 12, 8, 'rocket', level=1, health=1000),
            make_role(22, 12, 12, 'rocket', level=1, health=1000),
        ]

    def _upgrade_map(self, state, copper=True, vendor=True, shop=True):
        zones = [Zone(Pos(6, 9), 'stone')]
        if copper:
            zones.append(Zone(Pos(9, 6), 'copper'))
        if vendor:
            zones.append(Zone(Pos(1, 11), 'vendor'))
        if shop:
            zones.append(Zone(Pos(1, 9), 'weaponShop'))
        state.map_info.zones = zones
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 3), ShopItem('stone', 1)]
        return state

    def _est(self, state, gold=None):
        if gold is not None:
            state.team_our.gold_num = gold
        return estimate_opening_upgrade(state, build_blocked_set(state), state.team_our.gold_num)

    def test_estimate_includes_mining_when_broke_and_empty(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        est = self._est(state, gold=0)
        self.assertTrue(est['ok'])
        self.assertEqual(est['status'], 'need_mine')
        self.assertEqual(est['ore'], 'copper')
        self.assertGreaterEqual(est['mine_rounds'], 20)
        self.assertGreater(est['total'], est['mine_rounds'])
        self.assertGreater(est['route_rounds'], 0)
        self.assertGreaterEqual(est['action_rounds'], 3)

    def test_estimate_mines_only_the_remaining_voucher_gap(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        state.team_our.roles[1].backpack = ['copper'] * 10
        est = self._est(state, gold=0)
        self.assertTrue(est['ok'])
        self.assertEqual(est['status'], 'need_mine')
        self.assertEqual(est['mine_rounds'], 10)
        self.assertEqual(est['inventory_sale_value'], 50)

    def test_estimate_picks_higher_price_ore_when_total_rounds_are_shorter(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        state.map_info.zones.append(Zone(Pos(12, 6), 'iron'))
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 50), ShopItem('stone', 1)]
        est = self._est(state, gold=0)
        self.assertTrue(est['ok'])
        self.assertEqual(est['status'], 'need_mine')
        self.assertEqual(est['ore'], 'iron')
        self.assertEqual(est['mine_rounds'], 2)

    def test_estimate_sells_existing_metal_without_mining(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        state.team_our.roles[1].backpack = ['copper'] * 20
        est = self._est(state, gold=0)
        self.assertTrue(est['ok'])
        self.assertEqual(est['status'], 'sell_inventory')
        self.assertEqual(est['mine_rounds'], 0)
        self.assertGreaterEqual(est['inventory_sale_value'], 100)
        self.assertGreaterEqual(est['action_rounds'], 3)

    def test_estimate_gold_ready_skips_mine_and_sell(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        est = self._est(state, gold=130)
        self.assertTrue(est['ok'])
        self.assertEqual(est['status'], 'gold_ready')
        self.assertEqual(est['mine_rounds'], 0)
        self.assertEqual(est['funding_deficit'], 0)
        self.assertGreaterEqual(est['action_rounds'], 2)

    def test_estimate_held_voucher_skips_mine_sell_buy(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        state.team_our.roles[1].backpack = ['WeaponUpgradeVoucher1']
        est = self._est(state, gold=0)
        self.assertTrue(est['ok'])
        self.assertEqual(est['status'], 'have_voucher')
        self.assertEqual(est['mine_rounds'], 0)
        self.assertEqual(est['action_rounds'], 1)

    def test_estimate_mine_unreachable(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state, copper=False)
        est = self._est(state, gold=0)
        self.assertFalse(est['ok'])
        self.assertIsNone(est['total'])
        self.assertEqual(est['fallback_reason'], 'mine_unreachable')

    def test_estimate_vendor_unreachable(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state, vendor=False)
        state.team_our.roles[1].backpack = ['copper'] * 20
        est = self._est(state, gold=0)
        self.assertFalse(est['ok'])
        self.assertEqual(est['fallback_reason'], 'vendor_unreachable')

    def test_estimate_shop_unreachable(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state, shop=False)
        est = self._est(state, gold=130)
        self.assertFalse(est['ok'])
        self.assertEqual(est['fallback_reason'], 'shop_unreachable')

    def test_estimate_backpack_too_small_to_mine_enough(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        state.team_our.roles[1].back_pack_capability = 2
        state.team_our.roles[2].back_pack_capability = 2
        est = self._est(state, gold=0)
        self.assertFalse(est['ok'])
        self.assertEqual(est['fallback_reason'], 'backpack_capacity')
        self.assertGreaterEqual(est['mine_rounds'], 20)

    def test_dead_weapon_not_counted_as_three(self):
        state = opening_state()
        state.round_no = 20
        self._rockets(state)
        state.team_our.roles[-1].health = 0
        V1Strategy(BasicActionValidator()).decide(state)
        phase = next(e for e in state.decision_events if e['code'] == 'opening_phase')
        self.assertEqual(phase['weapons'], 2)
        self.assertEqual(phase['phase'], '武器')

    def test_dead_level_two_weapon_does_not_set_upgraded_once(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        state.team_our.roles.append(make_role(29, 11, 10, 'rocket', level=2, health=0))
        self._upgrade_map(state)
        V1Strategy(BasicActionValidator()).decide(state)
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertFalse(budget['upgraded'])
        self.assertFalse(budget.get('upgrade_funded'))
        self.assertEqual(budget.get('opening_stage'), 'BUILD_SURVIVAL_WALL')

    def test_dead_wall_reenters_missing(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        cell = primary_wall_plan(state, state.team_our.roles[0])[0]
        state.team_our.roles.append(make_role(99, cell[0], cell[1], 'wall', health=0, level=1))
        V1Strategy(BasicActionValidator()).decide(state)
        phase = next(e for e in state.decision_events if e['code'] == 'opening_phase')
        self.assertIn(cell, [tuple(p) for p in phase['wall_missing']])

    def test_day1_remaining_rounds(self):
        self.assertEqual(day_rounds_remaining(0), 70)
        self.assertEqual(day_rounds_remaining(20), 50)

    def test_day2_remaining_rounds(self):
        self.assertEqual(day_rounds_remaining(140), 60)
        self.assertEqual(day_rounds_remaining(70), 0)
        self.assertEqual(day_rounds_remaining(129), 0)

    def test_day1_defense_slack_is_not_official_night(self):
        from src.agent.brain import DAY_ROUNDS
        from src.agent.opening import (
            DAY1_L2_GUNNER_READY_CYCLE, DAY1_OTHER_READY_CYCLE,
            defense_ready_cycle, defense_rounds_remaining, first_night_economy_open,
        )
        state = opening_state()
        self._rockets(state)
        state.round_no = 69
        self.assertEqual(day_rounds_remaining(69), 1)
        self.assertEqual(defense_ready_cycle(state), DAY_ROUNDS)
        self.assertEqual(defense_rounds_remaining(state), 1)
        self.assertFalse(first_night_economy_open(state))
        self.assertGreater(DAY1_OTHER_READY_CYCLE, DAY_ROUNDS)
        self.assertGreater(DAY1_L2_GUNNER_READY_CYCLE, DAY_ROUNDS)
        next(r for r in state.team_our.roles if r.role_type == 'rocket').level = 2
        self.assertEqual(defense_ready_cycle(state), DAY_ROUNDS)
        from src.agent.opening import assign_weapons
        mapping = assign_weapons(state)
        l2 = next(r for r in state.team_our.roles if r.role_type == 'rocket' and (r.level or 1) >= 2)
        gunner = next(role for role in state.team_our.roles if mapping.get(role.id) and mapping[role.id].id == l2.id)
        other = next(role for role in state.team_our.roles if mapping.get(role.id) and mapping[role.id].id != l2.id)
        self.assertEqual(defense_rounds_remaining(state, gunner), 1)
        self.assertEqual(defense_rounds_remaining(state, other), 1)

    def test_first_night_without_robots_stops_opening_economy(self):
        state = opening_state()
        self._rockets(state)
        state.round_no = 72
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.gold_num = 130
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(e['code'] == 'opening_phase' for e in state.decision_events))
        self.assertFalse(any(c.get('action') in ('collect', 'buy', 'sell') for c in commands.values()))
        self.assertTrue(commands)

    def test_night_does_not_restart_opening_economy(self):
        state = opening_state()
        self._rockets(state)
        state.round_no = 80
        self.assertEqual(plan_opening(state), {})
        self.assertTrue(any(e['code'] == 'opening_night_guard' for e in state.decision_events))
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(e['code'] == 'opening_phase' for e in state.decision_events))
        self.assertFalse(any(c.get('action') in ('collect', 'buy', 'sell') for c in commands.values()))

    def test_first_night_robots_cut_opening_economy(self):
        from src.agent.protocol import RobotRole
        state = opening_state()
        self._rockets(state)
        state.round_no = 72
        state.robot.roles = [RobotRole(100, Pos(20, 10), 'largeRobot', 100)]
        self.assertEqual(plan_opening(state), {})
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(e['code'] == 'opening_phase' for e in state.decision_events))
        self.assertFalse(any(c.get('action') in ('collect', 'buy', 'sell') for c in commands.values()))

    def test_gold_ready_still_prioritizes_day1_walls(self):
        state = opening_state()
        self._rockets(state)
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 15, 3, 130, False, build_blocked_set(state))
        self.assertFalse(budget['allow_upgrade'])
        self.assertFalse(budget.get('allow_income_mine'))
        self.assertTrue(budget.get('upgrade_funded'))
        self.assertTrue(budget['allow_walls'])

    def test_upgrade_too_late_stops_metal_mining(self):
        state = opening_state()
        self._rockets(state)
        self._upgrade_map(state)
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 8, 3, 0, False, build_blocked_set(state))
        self.assertFalse(budget['allow_mine'])
        self.assertFalse(budget['allow_sell'])
        self.assertTrue(budget['allow_walls'])
        self.assertFalse(budget.get('allow_income_mine'))
        self.assertEqual(budget.get('opening_phase'), 'SURVIVAL_WALL')


class SurvivalWallAndIdleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest('第一天改由 opening_stage 状态机调度，连续轨迹见 tests.test_opening_fsm')

    def _rockets(self, state, level=1, health=1000):
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=level, health=health),
            make_role(21, 12, 8, 'rocket', level=level, health=health),
            make_role(22, 12, 12, 'rocket', level=level, health=health),
        ]

    def _economy_map(self, state, quotes=True):
        state.map_info.zones = [
            Zone(Pos(6, 9), 'stone'),
            Zone(Pos(9, 6), 'copper'),
            Zone(Pos(12, 6), 'iron'),
            Zone(Pos(1, 9), 'weaponShop'),
            Zone(Pos(1, 11), 'vendor'),
        ]
        if quotes:
            state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 3), ShopItem('stone', 1)]
        else:
            state.vendor_shop_list = []
        state.weapon_shop_list = [ShopItem('WeaponUpgradeVoucher1', 100)]
        return state

    def test_unfunded_no_metal_locks_survival_wall(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        V1Strategy(BasicActionValidator()).decide(state)
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertEqual(state.policy_memory.get('opening_commit'), 'survival_walls')
        self.assertFalse(budget['upgrade_funded'])
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['allow_stone_mine'])
        self.assertTrue(budget['allow_walls'])
        self.assertEqual(budget['opening_phase'] if 'opening_phase' in budget else 'SURVIVAL_WALL',
                         budget.get('fallback_reason') and 'SURVIVAL_WALL' or 'SURVIVAL_WALL')
        phase = next(e for e in state.decision_events if e['code'] == 'opening_phase')
        self.assertEqual(phase['phase'], 'SURVIVAL_WALL')

    def test_still_need_mining_does_not_mine_copper(self):
        state = opening_state()
        state.round_no = 12
        state.team_our.gold_num = 20
        self._rockets(state)
        self._economy_map(state)
        state.team_our.roles[1].backpack = ['copper'] * 2
        commands = V1Strategy(BasicActionValidator()).decide(state)
        ores = {
            (state.policy_memory.get('mine_targets') or {}).get(str(rid), {}).get('ore')
            for rid in (1, 2)
        }
        self.assertNotIn('copper', ores)
        self.assertNotIn('iron', ores)
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(any((commands.get(rid) or {}).get('action') in ('move', 'collect', 'build', 'sell')
                            for rid in (1, 2)))

    def test_two_packs_cover_voucher_sells_and_other_builds(self):
        state = opening_state()
        state.round_no = 12
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.team_our.roles[1].pos = Pos(1, 10)
        state.team_our.roles[1].backpack = ['copper'] * 12
        state.team_our.roles[2].backpack = ['copper'] * 10 + ['stone'] * 4
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(c.get('action') == 'sell' for c in commands.values()))
        other = commands.get(2) or {}
        self.assertIn(other.get('action'), ('move', 'build', 'collect'))
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertTrue(budget['upgrade_funded'] or budget.get('funding_reason') == 'inventory_covers')

    def test_unknown_quote_sells_once_without_mining(self):
        state = opening_state()
        state.round_no = 12
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state, quotes=False)
        state.team_our.roles[1].pos = Pos(1, 10)
        state.team_our.roles[1].backpack = ['copper'] * 8
        commands = V1Strategy(BasicActionValidator()).decide(state)
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertEqual(budget.get('funding_reason'), 'cashout_pending')
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(any(c.get('action') == 'sell' for c in commands.values()))
        ores = {
            (state.policy_memory.get('mine_targets') or {}).get(str(rid), {}).get('ore')
            for rid in (1, 2)
        }
        self.assertNotIn('copper', ores)

    def test_after_cashout_still_short_locks_survival(self):
        state = opening_state()
        state.round_no = 13
        state.team_our.gold_num = 10
        self._rockets(state)
        self._economy_map(state)
        state.policy_memory['cashout_pending'] = {'round': 12, 'reason': 'sale_value_unknown'}
        V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(state.policy_memory.get('opening_commit'), 'survival_walls')
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['allow_walls'])

    def test_gold_ready_buys_voucher(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(c.get('action') == 'buy' and c.get('name') == 'WeaponUpgradeVoucher1'
                            for c in commands.values()))

    def test_holding_voucher_uses_it_other_worker_walls(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.team_our.roles[1].backpack = ['WeaponUpgradeVoucher1']
        state.team_our.roles[2].backpack = ['stone'] * 4
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertIn(commands[1]['action'], ('move', 'use'))
        self.assertIn(commands[2]['action'], ('move', 'build'))

    def test_funded_but_unsafe_prefers_survival_wall(self):
        state = opening_state()
        state.round_no = 62
        state.team_our.gold_num = 130
        self._rockets(state)
        self._economy_map(state)
        missing = primary_wall_plan(state, state.team_our.roles[0])[:8]
        budget = opening_time_budget(state, missing, 8, 3, 130, False, build_blocked_set(state))
        self.assertTrue(budget['upgrade_funded'])
        self.assertFalse(budget['upgrade_safe'])
        self.assertTrue(budget['allow_walls'])
        self.assertFalse(budget['allow_income_mine'])
        self.assertEqual(budget.get('fallback_reason'), 'insufficient_time_for_upgrade_and_survival_wall')

    def test_after_first_upgrade_keeps_walls(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        next(r for r in state.team_our.roles if r.role_type == 'rocket').level = 2
        budget = opening_time_budget(
            state, primary_wall_plan(state, state.team_our.roles[0])[:8],
            50, 3, 0, 1, build_blocked_set(state),
        )
        self.assertTrue(budget['allow_walls'])
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['required_done'])

    def test_second_upgrade_cannot_disable_walls(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 250
        self._rockets(state)
        next(r for r in state.team_our.roles if r.role_type == 'rocket').level = 2
        budget = opening_time_budget(
            state, primary_wall_plan(state, state.team_our.roles[0])[:8],
            50, 3, 250, 1, build_blocked_set(state),
        )
        self.assertTrue(budget['allow_walls'])
        self.assertTrue(budget['allow_upgrade'])
        self.assertFalse(budget['allow_income_mine'])

    def test_survival_lock_does_not_resume_copper_when_gold_appears(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        self._economy_map(state)
        state.policy_memory['opening_commit'] = 'survival_walls'
        V1Strategy(BasicActionValidator()).decide(state)
        ores = {
            (state.policy_memory.get('mine_targets') or {}).get(str(rid), {}).get('ore')
            for rid in (1, 2)
        }
        self.assertNotIn('copper', ores)
        self.assertNotIn('iron', ores)
        self.assertEqual(state.policy_memory.get('opening_commit'), 'survival_walls')
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertFalse(budget['allow_income_mine'])
        self.assertTrue(budget['allow_walls'])

    def test_survival_plan_seals_front_base_gap_first(self):
        from src.agent.opening import survival_wall_plan, wall_priority
        state = opening_state()
        self._rockets(state)
        base = state.team_our.roles[0]
        plan = survival_wall_plan(state, base)
        self.assertTrue(plan)
        self.assertEqual(wall_priority(state, base, plan[0]), 0)

    def test_safe_wall_keeps_gun_posts(self):
        from src.agent.opening import assign_weapons, safe_wall, survival_wall_plan
        state = opening_state()
        self._rockets(state)
        assignments = assign_weapons(state)
        blocked = build_blocked_set(state)
        for point in survival_wall_plan(state, state.team_our.roles[0])[:4]:
            self.assertTrue(safe_wall(state, point, blocked, assignments), point)

    def test_cycle_70_musters_instead_of_day_idle_fallback(self):
        state = opening_state()
        state.round_no = 70
        self._rockets(state)
        self.assertEqual(plan_opening(state), {})
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(e['code'] == 'opening_phase' for e in state.decision_events))
        self.assertFalse(any(c.get('action') in ('collect', 'buy', 'sell', 'build') for c in commands.values()))

    def test_dead_wall_not_counted_in_survival(self):
        from src.agent.opening import survival_wall_missing, survival_wall_plan
        state = opening_state()
        self._rockets(state)
        cell = survival_wall_plan(state, state.team_our.roles[0])[0]
        state.team_our.roles.append(make_role(99, cell[0], cell[1], 'wall', health=0, level=1))
        self.assertIn(cell, survival_wall_missing(state))

    def test_dead_weapon_not_counted_as_upgraded(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        state.team_our.roles.append(make_role(29, 11, 10, 'rocket', level=2, health=0))
        V1Strategy(BasicActionValidator()).decide(state)
        budget = next(e for e in state.decision_events if e['code'] == 'opening_time_budget')
        self.assertFalse(budget['required_done'])
        self.assertFalse(budget['upgraded'])

    def test_second_worker_takes_other_wall_or_stone(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.team_our.roles[1].backpack = ['stone'] * 4
        state.team_our.roles[2].backpack = []
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertIn(commands[1]['action'], ('move', 'build'))
        self.assertIn(commands[2]['action'], ('move', 'collect', 'build'))
        if commands[1]['action'] == 'build' and commands[2]['action'] == 'build':
            self.assertNotEqual(commands[1]['targetPos'], commands[2]['targetPos'])

    def test_stale_voucher_job_without_gold_is_released(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.worker_item_jobs[1] = {
            'kind': 'weapon', 'item': 'WeaponUpgradeVoucher1', 'target': (12, 10),
        }
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertNotIn(1, state.worker_item_jobs)
        self.assertIn(commands[1]['action'], ('move', 'collect', 'build'))
        self.assertNotEqual(commands[1].get('action'), None)

    def test_stalled_shop_job_replans_after_three_idle_rounds(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 130
        self._rockets(state)
        self._economy_map(state)
        worker = state.team_our.roles[1]
        worker.pos = Pos(1, 9)
        worker.back_pack_capability = 1
        worker.backpack = ['Medicine']
        state.worker_item_jobs[1] = {
            'kind': 'weapon', 'item': 'WeaponUpgradeVoucher1', 'target': (12, 10),
        }
        strategy = V1Strategy(BasicActionValidator())
        for turn in range(5):
            state.round_no = 20 + turn
            strategy.decide(state)
        self.assertNotIn(1, state.worker_item_jobs)

    def test_daytime_at_gun_leaves_to_build_survival_wall(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        worker = state.team_our.roles[1]
        worker.pos = Pos(11, 10)
        worker.backpack = ['stone'] * 4
        state.policy_memory['weapon_assignment'] = {'1': 20, '2': 21, '3': 22}
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertIn(commands[1]['action'], ('move', 'build'))
        if commands[1]['action'] == 'build':
            self.assertEqual(commands[1]['name'], 'wall')

    def test_unreachable_claim_releases_and_picks_another(self):
        from src.agent.opening import claim_opening_wall, assign_weapons
        state = opening_state()
        self._rockets(state)
        worker = state.team_our.roles[1]
        blocked = build_blocked_set(state)
        reserved, claimed = set(), {(13, 10)}
        state.policy_memory['opening_wall_targets'] = {'1': [13, 10]}
        cmd = claim_opening_wall(
            worker, state, [(13, 10), (13, 9), (13, 11)], blocked, reserved, claimed, assign_weapons(state),
        )
        self.assertTrue(cmd)
        self.assertNotEqual(state.policy_memory.get('opening_wall_targets', {}).get('1'), [13, 10])

    def test_stale_opening_wall_target_is_cleared(self):
        from src.agent.opening import claim_opening_wall, assign_weapons
        state = opening_state()
        self._rockets(state)
        worker = state.team_our.roles[1]
        worker.backpack = ['stone'] * 2
        blocked = build_blocked_set(state)
        state.policy_memory['opening_wall_targets'] = {'1': [99, 99]}
        claim_opening_wall(
            worker, state, [(13, 10), (13, 9)], blocked, set(), set(), assign_weapons(state),
        )
        self.assertNotEqual(state.policy_memory.get('opening_wall_targets', {}).get('1'), [99, 99])

    def test_full_metal_backpack_in_survival_goes_to_vendor(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.policy_memory['opening_commit'] = 'survival_walls'
        worker = state.team_our.roles[1]
        worker.pos = Pos(2, 11)
        worker.back_pack_capability = 6
        worker.backpack = ['copper'] * 6
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertIn(commands[1]['action'], ('move', 'sell'))
        self.assertNotEqual(commands.get(1), None)

    def test_stone_in_pack_builds_or_moves_to_gap(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        worker = state.team_our.roles[1]
        worker.backpack = ['stone'] * 3
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertIn(commands[1]['action'], ('move', 'build'))

    def test_empty_pack_goes_to_stone(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        worker = state.team_our.roles[1]
        worker.backpack = []
        commands = V1Strategy(BasicActionValidator()).decide(state)
        ore = (state.policy_memory.get('mine_targets') or {}).get('1', {}).get('ore')
        self.assertEqual(ore, 'stone')
        self.assertIn(commands[1]['action'], ('move', 'collect'))

    def test_unreachable_stone_logs_block_reason(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        state.map_info.zones = [Zone(Pos(1, 9), 'weaponShop'), Zone(Pos(1, 11), 'vendor')]
        worker = state.team_our.roles[1]
        worker.backpack = []
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(e['code'] in ('stone_mine_unreachable', 'worker_no_command', 'blocked_by_nonstone_inventory')
                            for e in state.decision_events))
        self.assertTrue(commands.get(1) is None or commands[1].get('action') in ('move', 'build'))

    def test_dead_worker_is_not_assigned(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.team_our.roles[1].health = 0
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertNotIn(1, commands)

    def test_dead_weapon_does_not_park_worker(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        state.team_our.roles[-3].health = 0
        worker = state.team_our.roles[1]
        worker.pos = Pos(11, 10)
        worker.backpack = ['stone'] * 3
        state.policy_memory['weapon_assignment'] = {'1': 20, '2': 21, '3': 22}
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(commands.get(1))
        self.assertIn(commands[1]['action'], ('move', 'build', 'collect'))

    def test_alive_workers_have_work_while_survival_missing(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        self._rockets(state)
        self._economy_map(state)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        for rid in (1, 2):
            self.assertIn(rid, commands)
            self.assertIn(commands[rid]['action'], ('move', 'collect', 'build', 'sell', 'drop'))
        self.assertFalse(any(e.get('invariant_violation') == 'worker_idle_with_survival_wall_missing'
                             and e.get('role_id') in (1, 2)
                             for e in state.decision_events))

